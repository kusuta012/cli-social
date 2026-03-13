from __future__ import annotations
import asyncio
import json
import logging
from typing import Callable, Awaitable
from cli_social.p2p.transport import accept, NoiseSession
from cli_social.p2p.dht import DHTNode
from cli_social.storage import Storage, DEFAULT_DB_PATH
from cli_social.p2p.utils import read_frame, write_frame
from pathlib import Path
logger = logging.getLogger(__name__)


MessageCallback = Callable[[str, str], Awaitable[None]]


class Daemon:
    def __init__(
        self,
        peer_id: str,
        private_key: bytes,
        username: str = "",
        listen_port: int = 9000,
        dht_port: int = 6969,
        bootstrap_nodes: list[tuple[str, int]] | None = None,
        on_message: MessageCallback | None = None,
        db_path: Path | None = None,
        relay_host: str | None = None,
        relay_port: int = 9100
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
        self._server: asyncio.Server | None = None
        self._dht: DHTNode | None = None
        self._storage: Storage | None = None
        self._running = False
        self._relay_reader: asyncio.StreamReader | None = None
        self._relay_writer: asyncio.StreamWriter | None = None
        self._relay_task: asyncio.Task | None = None
        self._relay_session_tasks: set[asyncio.Task] = set()

    # coding this at 1am , I need caffeine !!
    async def start(self) -> None:
        self._storage = await Storage.open(self.db_path or DEFAULT_DB_PATH)
        self._dht = DHTNode(
            peer_id=self.peer_id,
            port=self.dht_port,
            bootstrap_nodes=self.bootstrap_nodes
        )
        await self._dht.start()
        await self._dht.announce(
            username=self.username,
            listen_port=self.listen_port,
            host="127.0.0.1"
        )
        
        self._server = await asyncio.start_server(
            self._handle_connection,
            "0.0.0.0",
            self.listen_port
        )
        self._running = True
        logger.info(f"Daemon listening on {self.listen_port}")
        logger.info(f"peer id {self.peer_id[:16]}.....{self.peer_id[-8:]}")
        
        if self.relay_host:
            await self._connect_to_relay()
            
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
                    logger.info(f"incoming relay conn from {from_peer_id[:12]}")
                    await write_frame(writer, json.dumps({"type": "accept"}).encode())
                    
                    try:
                        session = await accept(reader=reader, writer=writer, our_peer_id=self.peer_id, our_private_key=self.private_key, remote_peer_id=from_peer_id)
                        async def _msg_handler(peer_id: str, content: str, message_id: int, _session: NoiseSession = session) -> None:
                            await self._on_message(peer_id, content, _session)
                        task = asyncio.create_task(session.listen(_msg_handler, on_receipt=None), name=f"relay_session_{from_peer_id[:12]}")
                        self._relay_session_tasks.add(task)
                        task.add_done_callback(self._relay_session_tasks.discard)
                    
                    except Exception as e:
                        logger.error(f"relay handshake failed with {from_peer_id[:12]}, err = {e}")
                elif msg_type == "error":
                    logger.warning(f"relay error {msg.get("reason")}")
                    
                                    
        except asyncio.IncompleteReadError:
            logger.info("relay closed conn")
        except Exception as e:
            logger.error(f"relay listener error {e}")
        finally:
            self._relay_reader = None
            self._relay_writer = None
            logger.info("relay listener stopped")
            
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
            
    async def _on_message(self, peer_id: str, content: str, session: NoiseSession) -> None:
        message_id = -1
        if self._storage:
            message_id = await self._storage.save_message(
                peer_id=peer_id,
                sender_peer_id=peer_id,
                content=content,
                is_outgoing=False
            )
        
        try:
            await session.send_receipt(message_id)
            logger.debug(f"sent receipt {message_id} to {peer_id[:12]}")
        except Exception as e:
            logger.warning(f"failed to send receipt: {e}")
            
        if self.on_message:
            await self.on_message(peer_id, content)