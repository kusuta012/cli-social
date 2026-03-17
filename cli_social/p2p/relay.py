from __future__ import annotations
import asyncio
import json
import logging
import secrets
import socket
from cli_social.p2p.utils import read_frame, write_frame
from cli_social.p2p.registry import fetch_and_vrfy_registry

logger = logging.getLogger(__name__)

STORE_MAX_MESSAGES = 100
PIPE_TIMEOUT = 60
MAX_CONCURRENT_CONN = 500


async def _write_msg(writer: asyncio.StreamWriter, msg: dict) -> None:
    await write_frame(writer, json.dumps(msg).encode())


async def _read_msg(reader: asyncio.StreamReader) -> dict:
    frame = await read_frame(reader)
    return json.loads(frame)


class RelayConnection:
    def __init__(self, peer_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.peer_id = peer_id
        self.reader = reader
        self.writer = writer
        self._alive = True
        self.is_listener: bool = False
        
    async def send_msg(self, msg: dict) -> None:
        await _write_msg(self.writer, msg)

    async def receive_msg(self) -> dict:
        return await _read_msg(self.reader)

    async def send_raw(self, data: bytes) -> None:
        await write_frame(self.writer, data)

    async def receive_raw(self) -> bytes:
        return await read_frame(self.reader)

    def close(self) -> None:
        self._alive = False
        self.writer.close()

    @property
    def alive(self) -> bool:
        return self._alive


class RelayServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 9100):
        self.host = host
        self.port = port
        host_hash = socket.gethostname()
        self.relay_id = f"relay_{host_hash[:8]}"
        self._online: dict[str, RelayConnection] = {}
        self._store: dict[str, list[bytes]] = {}
        self._server: asyncio.Server | None = None
        self._pending_pipes: dict[str, asyncio.Future[RelayConnection]] = {}
        self._mesh_conns: dict[str, RelayConnection] = {}
        self._mesh_presence: dict[str, str] = {}
        self._mesh_task: asyncio.Task | None = None
        self._me_addr: set[str] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port
        )
        logger.info(f"Relay listening on {self.host}:{self.port} with ID {self.relay_id}")
        self._mesh_task = asyncio.create_task(self._mesh_mngr())

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self._online.values()) + list(self._mesh_conns.values()):
            conn.close()
        self._online.clear()
        self._mesh_conns.clear()
        logger.info(
            "Relay stopped , You are no longer relaying messages, Please restart when possible , we really need these to keep our app running <3")

    async def run_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("start() needs to called ;-;")
        async with self._server:
            await self._server.serve_forever()
    
    async def _mesh_mngr(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                relays = await fetch_and_vrfy_registry(None, accept_community=False)
                for r in relays:
                    targ_id = r.get("id")
                    addr = r.get("address", "")

                    if targ_id == self.relay_id:
                        continue
                    
                    if addr in self._me_addr:
                        continue

                    if not addr.startswith("tcp://"):
                        continue
                    host, port = addr.replace("tcp://", "").split(":")
                    
                    if targ_id not in self._mesh_conns:
                        asyncio.create_task(self._dial_mesh_relay(host, int(port) , addr)) # blank commit for docker img
            
            except Exception as e:
                logger.debug(f"Mesh manager loop error: {e}")

            await asyncio.sleep(60)
    
    async def _dial_mesh_relay(self, host: str, port: int, addr_key: str) -> None:
        conn = None
        try:
            reader, writer = await asyncio.open_connection(host , port)
            reg_msg = {"type": "mesh_register", "relay_id": self.relay_id}
            await write_frame(writer, json.dumps(reg_msg).encode())
            
            ack_raw = await read_frame(reader)
            ack = json.loads(ack_raw)
            remote_relay_id = ack.get("relay_id", addr_key)
            
            if remote_relay_id == self.relay_id:
                logger.info("Its me")
                self._me_addr.add(addr_key)
                writer.close()
                await writer.wait_closed()
                return
            
            if ack.get("type") != "mesh_ok":
                writer.close()
                return
            
            
            conn = RelayConnection(peer_id=addr_key, reader=reader, writer=writer)
            self._mesh_conns[remote_relay_id] = conn
            logger.info(f"established mesh link to {remote_relay_id} | {host}:{port}")
            
            for local_peer in self._online.keys():
                await conn.send_msg({"type": "mesh_add", "peer_id": local_peer, "relay_id": self.relay_id})
                
            while conn.alive:
                msg = await conn.receive_msg()
                await self._handle_mesh_message(msg)
            
        except Exception as e:
            logger.debug(f"failed to connect to {host}:{port}, {e}")
        finally:
            if conn:
                for rid, c in list(self._mesh_conns.items()):
                    if c is conn:
                        self._mesh_conns.pop(rid, None)
                    conn.close()
    
    def _broadcast_mesh(self, msg: dict) -> None:
        for conn in self._mesh_conns.values():
            asyncio.create_task(conn.send_msg(msg))
            
    async def _handle_mesh_message(self, msg:dict) -> None:
        msg_type = msg.get("type")
        peer_id = msg.get("peer_id")
        relay_id = msg.get("relay_id")
        
        if msg_type == "mesh_add" and peer_id and relay_id:
            self._mesh_presence[peer_id] = relay_id
        elif msg_type == "mesh_remove" and peer_id:
            self._mesh_presence.pop(peer_id, None)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_addr = writer.get_extra_info("peername")
        
        if len(self._online) >= MAX_CONCURRENT_CONN:
            logger.warning(f"relay at capacity!, dropping conn from {peer_addr}")
            writer.close()
            return
            
        conn: RelayConnection | None = None
        logger.info(f"new conn from {peer_addr}")

        try:
            msg = await asyncio.wait_for(_read_msg(reader), timeout=10)
            msg_type = msg.get("type")
            
            if msg_type == "mesh_register":
                remote_relay_id = msg.get("relay_id")
                await _write_msg(writer, {"type": "mesh_ok", "relay_id": self.relay_id})
                
                if remote_relay_id == self.relay_id:
                    return
                
                conn = RelayConnection(peer_id=peer_addr[0], reader=reader, writer=writer)
                self._mesh_conns[remote_relay_id] = conn
                
                for local_peer in self._online.keys():
                    await conn.send_msg({"type": "mesh_add", "peer_id": local_peer, "relay_id": self.relay_id})

                try:
                    while conn.alive:
                        mesh_msg = await conn.receive_msg()
                        await self._handle_mesh_message(mesh_msg)
                except Exception:
                    pass
                finally:
                    self._mesh_conns.pop(remote_relay_id, None)
                    conn.close()
                return
            
            if msg_type == "pipe":
                session_id = msg.get("session_id", "")
                future = self._pending_pipes.get(session_id)
                if not future:
                    await _write_msg(writer, {"type": "error", "reason": "unknown session_id"})
                    return
                pipe_conn = RelayConnection(peer_id=f"pipe_{session_id[:8]}", reader=reader, writer=writer)
                await _write_msg(writer, {"type": "ok"})
                future.set_result(pipe_conn)
                await asyncio.Event().wait()
                return
            
            if msg_type != "register" or not msg.get("peer_id"):
                await _write_msg(writer, {"type": "error", "reason": "first message must register"})
                return

            conn = RelayConnection(peer_id=msg["peer_id"], reader=reader, writer=writer)
            conn.is_listener = msg.get("mode") == "listen"
            if conn.is_listener:
                self._online[conn.peer_id] = conn
                self._broadcast_mesh({"type": "mesh_add", "peer_id": conn.peer_id, "relay_id": self.relay_id})
            await conn.send_msg({"type": "ok"})
            logger.info(f"peer {conn.peer_id[:12]} registered (mode={msg.get('mode', 'send')})")
            await self._flush_stored(conn)
            
            if conn.is_listener:
                while conn.alive:
                    await asyncio.sleep(15)             # these bugs are getting too annoying
                    try:
                        await conn.send_msg({"type": "ping"})
                        pong = await asyncio.wait_for(conn.receive_msg(), timeout=10)
                        if pong.get("type") != "pong":
                            break
                    except Exception:
                        break
                return

            action = await conn.receive_msg()
            action_type = action.get("type")

            if action_type == "connect":
                await self._handle_connect(conn, action)
            elif action_type == "fetch":
                await self._flush_stored(conn)
            else:
                await conn.send_msg({"type": "error", "reason": f"unkown {action_type}"})

        except asyncio.IncompleteReadError:
            logger.info(f"peer {conn.peer_id[:12] if conn else peer_addr} disconnected")
        except asyncio.TimeoutError:
            logger.warning(f"peer {peer_addr} timedout during rgisteration")
        except Exception as e:
            logger.error(f"relay error for {conn.peer_id[:12] if conn else peer_addr} {e}")
        finally:
            if conn is not None and conn.is_listener:
                if self._online.get(conn.peer_id) is conn:
                    self._online.pop(conn.peer_id, None)
                    self._broadcast_mesh({"type": "mesh_remove", "peer_id": conn.peer_id})

    async def _handle_connect(self, sender: RelayConnection, msg: dict) -> None:
        target_peer_id = msg.get("to")
        if not target_peer_id:
            await sender.send_msg({"type": "error", "reason": "missing target"})
            return
        target = self._online.get(target_peer_id)
        
        if target and target.alive:
            session_id = secrets.token_hex(8)
            future: asyncio.Future[RelayConnection] = asyncio.get_event_loop().create_future()
            self._pending_pipes[session_id] = future
            
            try:
                await target.send_msg({"type": "incoming", "from": sender.peer_id, "session_id": session_id})
                logger.info(f"notified {target.peer_id[:12]} of incoming from {sender.peer_id[:12]}")
                try:
                    pipe_conn = await asyncio.wait_for(future, timeout=PIPE_TIMEOUT)
                except asyncio.TimeoutError:
                    await sender.send_msg({"type": "error", "reason": "listener did not connect for pipe"})
                    return
                
                await sender.send_msg({"type": "ok"})
                await self._pipe(sender, pipe_conn) # too many logs , needed for debugging :)
                
            except Exception as e:
                logger.error(f"connect error {e}")
                await sender.send_msg({"type": "error", "reason": str(e)})
            finally:
                self._pending_pipes.pop(session_id, None)
            
        else:
            await sender.send_msg({"type": "stored", "message_id": msg.get("message_id", "")})
            try:
                payload = await asyncio.wait_for(sender.receive_raw(), timeout=PIPE_TIMEOUT)
                self._store_payload(target_peer_id, payload)
                logger.debug(f"stored payload for offline peer {target_peer_id[:12]}")
            except asyncio.TimeoutError:
                logger.warning(f"sender {sender.peer_id[:12]} timed out , oh oh did not receive payload")

    async def _pipe(self, a: RelayConnection, b: RelayConnection) -> None:
        async def forward(src: RelayConnection, dst: RelayConnection) -> None:
            try:
                while src.alive and dst.alive:
                    frame = await src.receive_raw()
                    await dst.send_raw(frame)
            except asyncio.IncompleteReadError:
                pass
            except Exception as e:
                logger.error(f"pipe error {src.peer_id[:12]} > {dst.peer_id[:12]}, error {e}")
            finally:
                dst.close()
                src.close()
                
        await asyncio.gather(forward(a, b), forward(b, a), return_exceptions=True)

    def _store_payload(self, peer_id: str, payload: bytes) -> None:
        queue = self._store.setdefault(peer_id, [])
        queue.append(payload)
        if len(queue) > STORE_MAX_MESSAGES:
            queue.pop(0)
            logger.warning(f"store full for {peer_id[:12]}, dropping oldest msg")

    
    def _unregister(self, conn: RelayConnection) -> None:
        self._online.pop(conn.peer_id, None)
        conn.close()
        logger.info(f"peer {conn.peer_id[:12]} unregisterd")


    async def _flush_stored(self, conn: RelayConnection) -> None:
        messages = self._store.pop(conn.peer_id, [])
        if not messages:
            return
        logger.debug(f"flushing {len(messages)} stored messages to {conn.peer_id[:12]}")
        for msg in messages:
            try:
                wrap = {"type": "stored_message", "payload": msg.hex()}
                await conn.send_msg(wrap)
            except Exception as e:
                logger.error(f"faiiled to flush {conn.peer_id[:12], {e}}")
                self._store[conn.peer_id] = messages[messages.index(msg):]
                break
            
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    # I need max logging ahhhh
    
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler("relay.log")])
    
    async def _main():
        server = RelayServer(host=args.host, port=args.port)
        await server.start()
        logger.info(f"relay running on {args.host}:{args.port}")
        await server.run_forever()
    asyncio.run(_main())