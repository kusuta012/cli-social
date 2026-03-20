from __future__ import annotations
import asyncio
import json
import logging
from re import S
import socket
import hashlib
import os
import aiosqlite
import secrets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cli_social.p2p.registry import fetch_and_vrfy_registry
from cli_social.p2p.utils import read_frame, write_frame

logger = logging.getLogger(__name__)

STORE_MAX_MESSAGES = 100
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
        self.msg_count_wind = 0
        self.wind_start = asyncio.get_event_loop().time()
        
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
        self._db: aiosqlite.Connection | None = None
        self._server: asyncio.Server | None = None
        self._mesh_conns: dict[str, RelayConnection] = {}
        self._mesh_presence: dict[str, str] = {}
        self._mesh_task: asyncio.Task | None = None
        self._me_addr: set[str] = set()
        self._pending_dials: set[str] = set()
        self._pending_acks: dict[str, list[dict]] = {}
        self._trusted_relay_ids: set[str] = set()
        self._ip_conns: dict[str, int] = {}
        self._max_per_ip: int = 5
        self._bg_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        db_path = os.getenv("RELAY_DB_PATH", "relay.db")
        self._db = await aiosqlite.connect(db_path)
        await self._db.execute("CREATE TABLE IF NOT EXISTS store (id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id TEXT NOT NULL, payload BLOB NOT NULL, message_id TEXT)")
        try:
            await self._db.execute("ALTER TABLE store ADD COLUMN message_id TEXT")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass
        await self._db.execute("CREATE TABLE IF NOT EXISTS pending_acks (id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id TEXT NOT NULL, from_id TEXT NOT NULL, message_id TEXT NOT NULL)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_store_peer ON store(peer_id)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_acks_peer ON pending_acks(peer_id)")
        await self._db.commit()
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port
        )
        logger.info(f"Relay listening on {self.host}:{self.port} with ID {self.relay_id}")
        self._mesh_task = asyncio.create_task(self._mesh_mngr())

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
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
                self._trusted_relay_ids = {r.get("id") for r in relays if r.get("id")}
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
                    
                    if targ_id not in self._mesh_conns and targ_id not in self._pending_dials:
                        self._pending_dials.add(targ_id)
                        task = asyncio.create_task(self._dial_mesh_relay(host, int(port) , addr, targ_id))
                        self._bg_tasks.add(task)
                        task.add_done_callback(self._bg_tasks.discard)

            except Exception as e:
                logger.debug(f"Mesh manager loop error: {e}")

            await asyncio.sleep(60)
    
    async def _dial_mesh_relay(self, host: str, port: int, addr_key: str, targ_id: str) -> None:
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
            self._pending_dials.discard(targ_id)
            rem_id = locals().get('remote_relay_id')
            if rem_id: 
                for pid, rid in list(self._mesh_presence.items()):
                    if rid == rem_id:
                        self._mesh_presence.pop(pid, None)
            if conn:
                for rid, c in list(self._mesh_conns.items()):
                    if c is conn:
                        self._mesh_conns.pop(rid, None)
                conn.close()
    
    def _broadcast_mesh(self, msg: dict) -> None:
        for conn in self._mesh_conns.values():
            task = asyncio.create_task(conn.send_msg(msg))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            
            
    async def _handle_mesh_message(self, msg:dict) -> None:
        msg_type = msg.get("type")
        peer_id = msg.get("peer_id")
        relay_id = msg.get("relay_id")
        
        if msg_type == "mesh_add" and peer_id and relay_id:
            self._mesh_presence[peer_id] = relay_id
            if not self._db:
                return
            async with self._db.execute("SELECT id, payload, message_id FROM store WHERE peer_id = ?", (peer_id,)) as cur:
                stored = await cur.fetchall()
            remote_conn = self._mesh_conns.get(relay_id)
            if stored and remote_conn and remote_conn.alive:
                s_ids = []
                for r_id, p, m_id in stored:
                        wrap = {"type": "mesh_stored_forward", "to": peer_id, "payload": p.hex(), "message_id": m_id}
                        t = asyncio.create_task(remote_conn.send_msg(wrap))
                        self._bg_tasks.add(t)
                        t.add_done_callback(self._bg_tasks.discard)
                        s_ids.append(r_id)
                if s_ids:
                    await self._db.execute(f"DELETE FROM store WHERE id IN ({','.join('?' for _ in s_ids)})", s_ids)
                    await self._db.commit()
            async with self._db.execute("SELECT id, from_id, message_id FROM pending_acks WHERE peer_id = ?", (peer_id,)) as cur:
                acks = await cur.fetchall()
            if acks and remote_conn and remote_conn.alive:
                a_ids = []
                for a_id, frm, m_id in acks:
                    wrap = {"type": "mesh_ack", "to": peer_id, "from": frm, "message_id": m_id}
                    t = asyncio.create_task(remote_conn.send_msg(wrap))
                    self._bg_tasks.add(t)
                    t.add_done_callback(self._bg_tasks.discard)
                    a_ids.append(a_id)
                if a_ids:
                    await self._db.execute(f"DELETE FROM pending_acks WHERE id IN ({','.join('?' for _ in a_ids)})", a_ids)
                    await self._db.commit()
        elif msg_type == "mesh_remove" and peer_id:
            self._mesh_presence.pop(peer_id, None)
        elif msg_type == "mesh_forward":
            target_id = msg.get("to")
            from_id = msg.get("from")
            payload_hex = msg.get("payload")
            msg_id = msg.get("message_id")
            target = self._online.get(target_id)
            if target and target.alive:
                await target.send_msg({"type": "push", "from": from_id, "payload": payload_hex, "message_id": msg_id})
            else:
                self._store_payload(target_id, bytes.fromhex(payload_hex), msg_id=msg_id or "")
        elif msg_type == "mesh_stored_forward":
            target_id = msg.get("to")
            payload_hex = msg.get("payload")
            msg_id = msg.get("message_id")
            target = self._online.get(target_id)
            if target and target.alive:
                wrap = {"type": "stored_message", "payload": payload_hex, "message_id": msg_id}
                t = asyncio.create_task(target.send_msg(wrap))
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)
            else:
                self._store_payload(target_id, bytes.fromhex(payload_hex), msg_id=msg_id or "")
        elif msg_type == "mesh_ack":
            target_id = msg.get("to")
            from_id = msg.get("from")
            msg_id = msg.get("message_id")
            target = self._online.get(target_id)
            if target and target.alive:
                await target.send_msg({"type": "delivery_ack", "from": from_id, "message_id": msg_id})
            else:
                self._store_pending_ack(target_id, {"from": from_id, "message_id": msg_id})

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_addr = writer.get_extra_info("peername")
        
        if len(self._online) >= MAX_CONCURRENT_CONN:
            logger.warning(f"relay at capacity!, dropping conn from {peer_addr}")
            writer.close()
            return
            
        client_ip = peer_addr[0] if peer_addr else "unknown"
        if self._ip_conns.get(client_ip, 0) >= self._max_per_ip:
            logger.warning(f"rate limit, too many connections from {client_ip}")
            writer.close()
            return
        self._ip_conns[client_ip] = self._ip_conns.get(client_ip, 0) + 1
        conn: RelayConnection | None = None
        logger.info(f"new conn from {peer_addr}")

        try:
            msg = await asyncio.wait_for(_read_msg(reader), timeout=10)
            msg_type = msg.get("type")
            
            if msg_type == "mesh_register":
                await self._handle_mesh_reg(msg, reader, writer, peer_addr)
                return
            
            if msg_type != "register" or not msg.get("peer_id") or not msg.get("public_key"):
                await _write_msg(writer, {"type": "error", "reason": "first message must register"})
                return
            peer_id = msg["peer_id"]
            public_key_hex = msg["public_key"]

            try:
                pub_bytes = bytes.fromhex(public_key_hex)
                if hashlib.sha256(pub_bytes).hexdigest() != peer_id:
                    await _write_msg(writer, {"type": "error", "reason": "public key does not match peer id"})
                    return
            except ValueError:
                await _write_msg(writer, {"type": "error", "reason": "invalid public key"})
                return
            
            nonce = secrets.token_hex(32)
            await _write_msg(writer, {"type": "challenge", "nonce": nonce})

            try:
                resp = await asyncio.wait_for(_read_msg(reader), timeout=10)
            except asyncio.TimeoutError:
                await _write_msg(writer, {"type": "error", "reason": "challenge timeout"})
                return
            
            if resp.get("type") != "challenge_response" or not resp.get("signature"):
                await _write_msg(writer, {"type": "error", "reason": "invalid challenge response"})
                return
            
            try:
                ed_pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
                auth_payload = f"cli-sxcl-relay-auth-v1:{nonce}".encode()
                ed_pub.verify(bytes.fromhex(resp["signature"]), auth_payload)
            except Exception:
                await _write_msg(writer, {"type": "error", "reason": "auth failed"})
                return

            conn = RelayConnection(peer_id=msg["peer_id"], reader=reader, writer=writer)
            self._online[conn.peer_id] = conn
            self._broadcast_mesh({"type": "mesh_add", "peer_id": conn.peer_id, "relay_id": self.relay_id})
            await conn.send_msg({"type": "ok"})
            await self._flush_stored(conn)
            await self._flush_pending_acks(conn)

            while conn.alive:
                try:
                    action = await asyncio.wait_for(conn.receive_msg(), timeout=70)
                    action_type = action.get("type")

                    if action_type == "publish":
                        now = asyncio.get_event_loop().time()
                        if now - conn.wind_start > 45:
                            conn.wind_start = now
                            conn.msg_count_wind = 0
                        
                        conn.msg_count_wind += 1
                        if conn.msg_count_wind > 45:
                            logger.warning(f"peer {conn.peer_id[:12]} exceeded rate limit, dropping")
                            await conn.send_msg({"type": "error", "reason": "rate limit exceeded"})
                            conn.close()
                            break
                        await self._handle_publish(conn, action)
                    elif action_type == "client_ack":
                        await self._handle_client_ack(conn, action)
                    elif action_type == "pong":
                        continue
                except asyncio.TimeoutError:
                    await conn.send_msg({"type": "ping"})
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.error(f"peer {conn.peer_id[:12] if conn else peer_addr} disconnected")
        finally:
            self._ip_conns[client_ip] = max(0, self._ip_conns.get(client_ip, 0) - 1)
            if conn:
                self._online.pop(conn.peer_id, None)
                self._broadcast_mesh({"type": "mesh_remove", "peer_id": conn.peer_id})
                conn.close()
    
    async def _handle_publish(self, sender: RelayConnection, msg: dict) -> None:
        target_id = msg.get("to")
        payload = msg.get("payload")
        msg_id = msg.get("message_id")

        if not target_id or not payload:
            return
        
        target = self._online.get(target_id)
        if target and target.alive:
            await target.send_msg({"type": "push", "from": sender.peer_id, "payload": payload, "message_id": msg_id})
            await sender.send_msg({"type": "relay_ack", "message_id": msg_id, "status": "relayed"})
            return

        remote_relay_id = self._mesh_presence.get(target_id)
        if remote_relay_id:
            remote_conn = self._mesh_conns.get(remote_relay_id)
            if remote_conn and remote_conn.alive:
                await remote_conn.send_msg({"type": "mesh_forward", "to": target_id, "from": sender.peer_id, "payload": payload, "message_id": msg_id})
                await sender.send_msg({"type": "relay_ack", "message_id": msg_id, "status": "relayed"})
                return
        self._store_payload(target_id, bytes.fromhex(payload), msg_id=msg_id)
        await sender.send_msg({"type": "relay_ack", "message_id": msg_id, "status": "relayed"})
    
    async def _handle_client_ack(self, sender: RelayConnection, msg: dict) -> None:
        target_id = msg.get("to")
        msg_id = msg.get("message_id")
        if not target_id:
            return
        target = self._online.get(target_id)
        if target and target.alive:
            await target.send_msg({"type": "delivery_ack", "from": sender.peer_id, "message_id": msg_id})
        elif target_id in self._mesh_presence:
            remote_relay_id = self._mesh_presence[target_id]
            remote_conn = self._mesh_conns.get(remote_relay_id)
            if remote_conn and remote_conn.alive:
                await remote_conn.send_msg({"type": "mesh_ack", "to": target_id, "from": sender.peer_id, "message_id": msg_id })
            else:
                self._store_pending_ack(target_id, {"from": sender.peer_id, "message_id": msg_id})
        else:
            self._store_pending_ack(target_id, {"from": sender.peer_id, "message_id": msg_id})

    async def _handle_mesh_reg(self, msg: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, addr: tuple) -> None:
        remote_relay_id = msg.get("relay_id")
        if remote_relay_id not in self._trusted_relay_ids:
            logger.warning(f"rejected mesh connection from untrusted relay: {remote_relay_id}")
            await _write_msg(writer, {"type": "error", "reason": "not in trusted registry"})
            writer.close()
            return
        await _write_msg(writer, {"type": "mesh_ok", "relay_id": self.relay_id})

        if remote_relay_id == self.relay_id:
            writer.close()
            return
        
        conn = RelayConnection(peer_id=addr[0], reader=reader, writer=writer)
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
            for pid, rid in list(self._mesh_presence.items()):
                if rid == remote_relay_id:
                    self._mesh_presence.pop(pid, None)
            self._mesh_conns.pop(remote_relay_id, None)
            conn.close()
        
    def _store_payload(self, peer_id: str, payload: bytes, msg_id: str = "") -> None:
        async def _save():
            if not self._db:
                return
            await self._db.execute("INSERT INTO store (peer_id, payload, message_id) VALUES (?, ?, ?)", (peer_id, payload, msg_id))
            await self._db.commit()
        t = asyncio.create_task(_save())
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    def _store_pending_ack(self, peer_id: str, ack_data: dict) -> None:
        async def _save():
            if not self._db:
                return
            await self._db.execute("INSERT INTO pending_acks (peer_id, from_id, message_id) VALUES (?, ?, ?)", (peer_id, ack_data["from"], ack_data["message_id"]))
            await self._db.commit()
        t = asyncio.create_task(_save())
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
    
    async def _flush_pending_acks(self, conn: RelayConnection) -> None:
        if not self._db:
            return
        peer_id = conn.peer_id
        async with self._db.execute("SELECT id, from_id, message_id FROM pending_acks WHERE peer_id = ?", (peer_id,)) as cur:
            acks = await cur.fetchall()
        if acks:
            logger.debug(f"flushing {len(acks)} acks to {peer_id[:12]}")
            s_ids = []
            for a_id, frm, m_id in acks:
                try:
                    await conn.send_msg({"type": "delivery_ack", "from": frm, "message_id": m_id})
                    s_ids.append(a_id)
                except Exception as e:
                    logger.error(f"failed to flush ack to {peer_id[:12]}, {e}")
                    break
            if s_ids:
                await self._db.execute(f"DELETE FROM pending_acks WHERE id IN ({','.join('?' for _ in s_ids)})", s_ids)
                await self._db.commit()
    
    def _unregister(self, conn: RelayConnection) -> None:
        self._online.pop(conn.peer_id, None)
        conn.close()
        logger.info(f"peer {conn.peer_id[:12]} unregisterd")

    async def _flush_stored(self, conn: RelayConnection) -> None:
        if not self._db:
            return
        peer_id = conn.peer_id
        async with self._db.execute("SELECT id, payload, message_id FROM store WHERE peer_id = ?", (peer_id,)) as cur:
            stored = await cur.fetchall()
        if stored:
            logger.debug(f"flushing {len(stored)} stored to {peer_id[:12]}")
            s_ids = []
            for r_id, p, m_id in stored:
                try:
                    await conn.send_msg({"type": "stored_message", "payload": p.hex(), "message_id": m_id})
                    s_ids.append(r_id)
                except Exception as e:
                    logger.error(f"failed to flush msg to {peer_id[:12]}, {e}")
                    break
            if s_ids:
                await self._db.execute(f"DELETE FROM store WHERE id IN ({','.join('?' for _ in s_ids)})", s_ids)
                await self._db.commit()
    

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