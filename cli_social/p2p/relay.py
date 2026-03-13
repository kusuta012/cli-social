from __future__ import annotations
import asyncio
import json
import logging
from cli_social.p2p.utils import read_frame, write_frame

logger = logging.getLogger(__name__)

STORE_MAX_MESSAGES = 100
PIPE_TIMEOUT = 10


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
        self._online: dict[str, RelayConnection] = {}
        self._store: dict[str, list[bytes]] = {}
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port
        )
        logger.info(f"Relay listening sie on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self._online.values()):
            conn.close()
        self._online.clear()
        logger.info(
            f"Relay stopped , You are no longer relaying messages, Please start when possible , we really need these to keep our app running <3")

    async def run_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("start() needs to called ;-;")
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_addr = writer.get_extra_info("peername")
        conn: RelayConnection | None = None
        logger.info(f"new conn from {peer_addr}")

        try:
            msg = await asyncio.wait_for(_read_msg(reader), timeout=10)
            if msg.get("type") != "register" or not msg.get("peer_id"):
                await _write_msg(writer, {"type": "error", "reason": "first message must register"})
                return

            conn = RelayConnection(peer_id=msg["peer_id"], reader=reader, writer=writer)
            conn.is_listener = msg.get("mode") == "listen"
            self._online[conn.peer_id] = conn
            await conn.send_msg({"type": "ok"})
            logger.info(f"peer {conn.peer_id[:12]} registered (mode={msg.get('mode', 'send')})")
            await self._flush_stored(conn)
        
            if msg.get("mode") == "listen":
                while conn.alive:
                    await asyncio.sleep(30)
                    try:
                        await conn.send_msg({"type": "ping"})
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
            if conn and not getattr(conn, "is_listener", False):
                self._unregister(conn)

    async def _handle_connect(self, sender: RelayConnection, msg: dict) -> None:
        target_peer_id = msg.get("to")
        if not target_peer_id:
            await sender.send_msg({"type": "error", "reason": "missing target"})
            return
        target = self._online.get(target_peer_id)
        
        if target and target.alive:
            try:
                await target.send_msg({"type": "incoming", "from": sender.peer_id})
            except Exception as e:
                logger.warning(f"faile dot notify {target.peer_id[:12]}, err {e}")
                self._unregister(target)
                target = None

        if target and target.alive:
            try:
                ack = await asyncio.wait_for(target.receive_msg(), timeout=PIPE_TIMEOUT)
                if ack.get("type") != "accept":
                    await sender.send_msg({"type": "error", "reason": "target rejected conn"})
                    return
            except asyncio.TimeoutError:
                await sender.send_msg({"type": "error", "reason": "target did not respond"})
                return
            await sender.send_msg({"type": "ok"})
            logger.info(f"piping {sender.peer_id[:12]} | {target.peer_id[:12]}")
            await self._pipe(sender, target)
            self._online.pop(sender.peer_id, None)
        else:
            await sender.send_msg({"type": "stored", "message_id": msg.get("message_id", "")})
            logger.debug(f"target {target_peer_id[:12]} offline , waiting for payload, D")

            # this is so tuff
            try:
                payload = await asyncio.wait_for(sender.receive_raw(), timeout=PIPE_TIMEOUT)
                self._store_payload(target_peer_id, payload)
                logger.debug(f"stored payload for offline peer {target_peer_id[:12]}")
            except asyncio.TimeoutError:
                logger.warning(f"sender {sender.peer_id[:12]} timed out , oh oh did not receive payload")

    async def _pipe(self, a: RelayConnection, b: RelayConnection) -> None:
        async def forward(src: RelayConnection, dst: RelayConnection, close_dst: bool) -> None:
            try:
                while src.alive and dst.alive:
                    frame = await src.receive_raw()
                    await dst.send_raw(frame)
            except asyncio.IncompleteReadError:
                logger.info(f"pipe end {src.peer_id[:12]} disconnected")
            except Exception as e:
                logger.error(f"pipe error {src.peer_id[:12]} > {dst.peer_id[:12]}, error {e}")
            finally:
                if close_dst:
                    dst.close()
        await asyncio.gather(forward(a, b, close_dst=False), forward(b, a, close_dst=True), return_exceptions=True)
        logger.info(f"pipe closed {a.peer_id[:12]} | {b.peer_id[:12]}")

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
                await conn.send_raw(msg)
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