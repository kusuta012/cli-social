from __future__ import annotations
import asyncio
import json
import logging
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Callable, Awaitable
from cli_social.p2p.transport import accept, NoiseSession
from cli_social.p2p.dht import DHTNode
from cli_social.storage import Storage, DEFAULT_DB_PATH
from cli_social.p2p.utils import read_frame, write_frame
from cli_social.p2p.registry import fetch_and_vrfy_registry
from cli_social.p2p.transport import decrypt_offline_message
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

logger = logging.getLogger(__name__)


MessageCallback = Callable[[str, str], Awaitable[None]]


class Daemon:
    def __init__(
        self,
        peer_id: str,
        private_key: bytes,
        username: str = "",
        listen_port: int = 53241,
        dht_port: int = 6969,
        bootstrap_nodes: list[tuple[str, int]] | None = None,
        on_message: MessageCallback | None = None,
        db_path: Path | None = None,
        relay_host: str | None = None,
        relay_port: int | None = None,
        cached_relay: dict | None = None
    ):
        self.peer_id = peer_id
        self.private_key = private_key
        self.username = username
        self.listen_port = listen_port
        self.dht_port = dht_port
        self.bootstrap_nodes = bootstrap_nodes or []
        self.on_message = on_message
        self.db_path = db_path
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.cached_relay = cached_relay 
        self._server: asyncio.Server | None = None
        self._dht: DHTNode | None = None
        self._storage: Storage | None = None
        self._running = False
        self._relay_reader: asyncio.StreamReader | None = None
        self._relay_writer: asyncio.StreamWriter | None = None
        self._relay_task: asyncio.Task | None = None
        self._relay_session_tasks: set[asyncio.Task] = set()
        self._recent_message_ids = deque(maxlen=200)
        self.noise_pubkey_hex = ""

    # coding this at 1am , I need caffeine !!
    async def start(self) -> None:
        noise_priv_key = X25519PrivateKey.from_private_bytes(self.private_key)
        self.noise_pubkey_hex = noise_priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        
        self._storage = await Storage.open(self.db_path or DEFAULT_DB_PATH)
        self._dht = DHTNode(
            peer_id=self.peer_id,
            port=self.dht_port,
            bootstrap_nodes=self.bootstrap_nodes
        )
        await self._dht.start()
        asyncio.create_task(self._run_p2p_server())
        asyncio.create_task(self._run_relay_mngr())
        if self.relay_host:
            await self._connect_to_relay()
        else:
            await self._discover_and_connect_relay()
        
        hm_str = f"{self.relay_host}:{self.relay_port}" if self.relay_host else ""
        await self._dht.announce(
            username=self.username,
            noise_pubkey_hex=self.noise_pubkey_hex,
            home_relay=hm_str
        )
        self._running = True
        logger.info("Daemon startup intiated in the backgreound")
        logger.info(f"peer id {self.peer_id[:16]}.....{self.peer_id[-8:]}")
        
    
    # Idk how but on every night coding session I end up in this file
    async def _run_p2p_server(self):
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self.listen_port)
        logger.info(f"p2p server listening on {self.listen_port}")
        async with self._server:
            await self._server.serve_forever()
    
    async def _run_relay_mngr(self):
        if self.relay_host:
            await self._connect_to_relay()
        else:
            is_first_run = True
            while self._running:
                if is_first_run and self.cached_relay:
                    self.relay_host = self.cached_relay["host"]
                    self.relay_port = self.cached_relay["port"]
                    try:
                        await asyncio.wait_for(self._connect_to_relay(), timeout=3)
                    except Exception:
                        self.relay_host = None
                        self.cached_relay = None
                
                if not self._relay_writer:
                    await self._discover_and_connect_relay()
                    
                if self._relay_writer:
                    is_first_run = False
                    logger.info("announcing presence to DHT")
                    home_relay_str = f"{self.relay_host}:{self.relay_port}"
                    await self._dht.announce(username=self.username, noise_pubkey_hex=self.noise_pubkey_hex, home_relay=home_relay_str)
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(30)
                        
    async def _connect_to_relay(self) -> None:
        try:
            reader, writer = await asyncio.open_connection(self.relay_host, self.relay_port)
            reg = json.dumps({"type": "register", "peer_id": self.peer_id, "mode": "listen"}).encode()
            await write_frame(writer, reg)
            
            ack = json.loads(await read_frame(reader))
            if ack.get("type") != "ok":
                logger.error(f"relay register failed {ack.get('reason')}")
                writer.close()
                return
            self._relay_reader = reader
            self._relay_writer = writer
            logger.info(f"registered with relay at {self.relay_host}:{self.relay_port}")
            self._relay_task = asyncio.create_task(self._listen_relay(reader, writer), name="relay listener")
        except Exception as e:
            logger.error(f"could not connect {e}")

    async def _listen_relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while self._running:
                msg = json.loads(await read_frame(reader))
                msg_type = msg.get("type")
                
                if msg_type == "incoming":
                    from_peer_id = msg.get("from", "unknown")
                    session_id = msg.get("session_id", "")
                    logger.info(f"incoming relay conn from {from_peer_id[:12]}, session {session_id[:8]}")
                    
                    try:
                        pipe_reader, pipe_writer = await asyncio.open_connection(self.relay_host, self.relay_port)
                        await write_frame(pipe_writer, json.dumps({"type": "pipe", "session_id": session_id}).encode())
                        ack = json.loads(await read_frame(pipe_reader))
                        if ack.get("type") != "ok":
                            logger.error(f"pipe conn rejected {ack.get('reason')}")
                            pipe_writer.close()
                            continue
                        
                        session = await accept(reader=pipe_reader, writer=pipe_writer, our_peer_id=self.peer_id, our_private_key=self.private_key, remote_peer_id=from_peer_id)
                        
                        async def _msg_handler(peer_id: str, content: str, message_id: int, _session = session) -> None:
                            await self._on_message(peer_id, content, _session)
                        
                        task = asyncio.create_task(session.listen(_msg_handler, on_receipt=None), name=f"relay_session_{from_peer_id[:12]}")
                        self._relay_session_tasks.add(task)
                        task.add_done_callback(self._relay_session_tasks.discard)
                    
                    except Exception as e:
                        logger.error(f"failed to open pipe for {session_id[:8]}, err = {e}")
                
                elif msg_type == "stored_message":
                    logger.info("Received a stored msg from relay")
                    try:
                        payload_bytes = bytes.fromhex(msg["payload"])
                        decrypted_msg = await decrypt_offline_message(self.private_key, payload_bytes)
                        sender_peer_id = decrypted_msg.get("peer_id", "unknown-offline-sender")
                        await self._on_message(sender_peer_id, decrypted_msg["content"], None, decrypted_msg.get("client_message_id"))
                        logger.info("successfully processed stored msg")
                    except Exception as e:
                        logger.error(f"Failed to decrypt stored message {e}") 
                        
                elif msg_type == "ping":
                    logger.debug("relay keepalive ping")
                    await write_frame(writer, json.dumps({"type": "pong"}).encode())
                elif msg_type == "error":
                    logger.warning(f"relay error {msg.get('reason')}")
                    
                                    
        except asyncio.IncompleteReadError:
            logger.info("relay closed conn")
        except Exception as e:
            logger.error(f"relay listener error {e}")
        finally:
            self._relay_reader = None
            self._relay_writer = None
            logger.info(f"relay listener stopped at: {traceback.format_exc()}")
            if self._running:
                await asyncio.sleep(3)
                await self._connect_to_relay()
            
    async def run_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("call start first, dumbb")
        async with self._server:
            await self._server.serve_forever()
    
    async def stop(self) -> None:
        self._running = False
        if self._relay_task:
            self._relay_task.cancel()
        for task in list(self._relay_session_tasks):
            task.cancel()
        if self._relay_writer:
            self._relay_writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._dht:
            await self._dht.stop()
        if self._storage:
            await self._storage.close()
        logger.info("dameon stopped")
            
            
    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ) -> None:
        peer_addr = writer.get_extra_info("peername")
        logger.info(f"incoming from {peer_addr}")
        
        try:
            session: NoiseSession = await accept(
                reader=reader,
                writer=writer,
                our_peer_id=self.peer_id,
                our_private_key=self.private_key
            )
            logger.info(f"handhshake completed {peer_addr}")
            
            async def _msg_handler(peer_id: str, content: str, message_id: int, _session: NoiseSession = session) -> None:
                await self._on_message(peer_id, content, session)
            await session.listen(_msg_handler, on_receipt=None)
            
        except Exception as e:
            logger.error(f"connection from {peer_addr} failed {e}")
        finally:
            writer.close()
            
    async def _on_message(self, peer_id: str, content: str, session: NoiseSession | None, client_message_id: str | None = None) -> None:
        if client_message_id:
            if client_message_id in self._recent_message_ids:
                logger.debug(f"Duplicate msg {client_message_id[:8]} from {peer_id[:8]} dropped")
                return
            self._recent_message_ids.append(client_message_id)
            
        message_id = -1
        if self._storage:
            message_id = await self._storage.save_message(
                peer_id=peer_id,
                sender_peer_id=peer_id,
                content=content,
                is_outgoing=False
            )
        
        if session:
            try:
                await session.send_receipt(message_id)
                logger.debug(f"sent receipt {message_id} to {peer_id[:12]}")
            except Exception as e:
                logger.warning(f"failed to send receipt: {e}")
            
        if self.on_message:
            await self.on_message(peer_id, content)
    
    async def _discover_and_connect_relay(self) -> None:
        
        if self._dht is None:
            logger.error("DHT not running, cannot dicsover relays")
            return
        
        dht_payload = await self._dht.get_value("relays.v1")
            
        try:
            relays = await fetch_and_vrfy_registry(dht_payload, accept_community=False)
            if not relays:
                logger.error("No relays found in the verified registry")
                return
            
            tcp_relays = [r for r in relays if r.get("address", "").startswith("tcp://")]
            if not tcp_relays:
                logger.error("No valid relays found")
                return
            
            logger.info(f"found {len(tcp_relays)}, measuring latency..")
            
            async def measure_latency(relay: dict) -> tuple[dict, float]:
                addr = relay["address"].replace("tcp://", "")
                host, port = addr.split(":")
                start_time = time.perf_counter()
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=3)
                    writer.close()
                    await writer.wait_closed()
                    latency = time.perf_counter() - start_time
                    return relay, latency
                except Exception:
                    return relay, float('inf')
            
            ping_tasks =[measure_latency(r) for r in tcp_relays]
            results = await asyncio.gather(*ping_tasks)
            alive_relays =[(r,lat) for r, lat in results if lat != float('inf')]
            
            if not alive_relays:
                logger.error("all relays unreachable at the moment, please retry")
                return
            
            alive_relays.sort(key=lambda x: x[1])
            for r, lat in alive_relays:
                region = r.get("meta", {}).get("region", "unknown")
                logger.info(f"  {r['id']} ({region}), {lat * 100:.0f} ms")
            
            chosen = alive_relays[0][0]
            
            addr = chosen["address"].replace("tcp://", "")
            host, port = addr.split(":")
            
            self.relay_host = host
            self.relay_port = int(port)
            logger.info(f"Chose fasetest relay from registry {self.relay_host}:{self.relay_port}")
            
            await self._connect_to_relay()
            
        except Exception as e:
            logger.error(f"relay sequence failed {e}")