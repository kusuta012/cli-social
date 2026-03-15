from __future__ import annotations
import asyncio
import json
import logging
import uuid
import secrets
from datetime import datetime, timezone
from typing import Callable, Awaitable
from noise.connection import NoiseConnection, Keypair
from noise.backends.default import diffie_hellmans as DH
from cli_social.p2p.utils import read_frame, write_frame

logger = logging.getLogger(__name__)


MessageHandler = Callable[[str, str, int], Awaitable[None]]
ReceiptHandler = Callable[[int], Awaitable[None]]


class PeerOfflineError(ConnectionError):
    def __init__(self, message, writer) -> None:
        super().__init__(message)
        self.writer = writer


def generate_noise_keypair() -> tuple[bytes, bytes]:
    keypair = DH.ED25519().generate_keypair()
    return keypair.public_bytes, keypair.private.private_bytes_raw()


async def _do_handshake_initiator(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    our_private: bytes,
) -> NoiseConnection:
    noise = NoiseConnection.from_name(b"Noise_XX_25519_ChaChaPoly_SHA256")
    noise.set_as_initiator()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private)
    noise.start_handshake()

    msg1 = noise.write_message()
    await write_frame(writer, bytes(msg1))

    msg2 = await read_frame(reader)
    noise.read_message(msg2)

    msg3 = noise.write_message()
    await write_frame(writer, bytes(msg3))

    assert noise.handshake_finished, "Hanshake did not finish :("
    logger.debug("Noise handshake finished (init)")
    return noise


async def _do_handshake_responder(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    our_private: bytes
) -> NoiseConnection:

    noise = NoiseConnection.from_name(b"Noise_XX_25519_ChaChaPoly_SHA256")
    noise.set_as_responder()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private)
    noise.start_handshake()

    msg1 = await read_frame(reader)
    noise.read_message(msg1)

    msg2 = noise.write_message()
    await write_frame(writer, bytes(msg2))

    msg3 = await read_frame(reader)
    noise.read_message(msg3)

    assert noise.handshake_finished, "Hanshake did not finish :("
    logger.debug("Noise handshake finished (resp)")
    return noise


async def encrypt_for_offline(our_peer_id: str, our_private_key: bytes, their_pubkey: bytes, content: str, client_message_id: str):
    noise = NoiseConnection.from_name(b"Noise_XX_25519_ChaChaPoly_SHA256")
    noise.set_as_initiator()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private_key)
    noise.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, their_pubkey)
    noise.start_handshake()
    payload = json.dumps({
            "type": "message",
            "peer_id": our_peer_id,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_message_id": client_message_id
    }).encode()
    encrypted_message = noise.write_message(payload=payload)
    return bytes(encrypted_message)

async def decrypt_offline_message(our_private_key: bytes, encrypted_blob: bytes) -> dict:
    noise = NoiseConnection.from_name(b"Noise_XX_25519_ChaChaPoly_SHA256")
    noise.set_as_responder()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private_key)
    noise.start_handshake()
    
    decrypted_payload = noise.read_message(encrypted_blob)
    return json.loads(decrypted_payload)

class NoiseSession:
    def __init__(
        self,
        noise: NoiseConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        our_peer_id: str,
        remote_peer_id: str,
        via_relay: bool = False

    ):
        self._noise = noise
        self._reader = reader
        self._writer = writer
        self.our_peer_id = our_peer_id
        self.remote_peer_id = remote_peer_id
        self._via_relay = via_relay

    async def send(self, content: str, client_message_id: str) -> None:
        payload = json.dumps({
            "type": "message",
            "peer_id": self.our_peer_id,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_message_id": client_message_id
        }).encode()
        encrypted = self._noise.encrypt(payload)
        await write_frame(self._writer, encrypted)
        logger.debug(f"sent to {self.remote_peer_id[:12]} (relay={self._via_relay})")

    async def send_receipt(self, message_id: int) -> None:
        payload = json.dumps({
            "type": "receipt",
            "message_id": message_id
        }).encode()
        encrypted = self._noise.encrypt(payload)
        await write_frame(self._writer, encrypted)
        logger.debug(f"sent receipt for {message_id} to {self.remote_peer_id[:12]}")

    async def receive(self) -> dict:
        frame = await read_frame(self._reader)
        plaintext = self._noise.decrypt(frame)
        return json.loads(plaintext)

    async def listen(self, on_message: MessageHandler, on_receipt: ReceiptHandler | None = None) -> None:
        try:
            while True:
                msg = await self.receive()
                msg_type = msg.get("type", "message")

                if msg_type == "receipt":
                    if on_receipt and "message_id" in msg:
                        await on_receipt(msg["message_id"])
                elif msg_type == "message":
                    await on_message(msg["peer_id"], msg["content"], msg.get("message_id", -1))
        except asyncio.IncompleteReadError:
            logger.info(f"peer {self.remote_peer_id[:12]} disconnected")
        except Exception as e:
            logger.error(f"session error {e}")

    def close(self) -> None:
        self._writer.close()


async def connect(
    host: str,
    port: int,
    our_peer_id: str,
    our_private_key: bytes,
    their_peer_id: str,
) -> NoiseSession:
    logger.info(f"Connecting to {their_peer_id[:12]} at {host}:{port}")
    reader, writer = await asyncio.open_connection(host, port)
    noise = await _do_handshake_initiator(reader, writer, our_private_key)

    return NoiseSession(
        noise=noise,
        reader=reader,
        writer=writer,
        our_peer_id=our_peer_id,
        remote_peer_id=their_peer_id,
        via_relay=False,
    )


async def connect_via_relay(our_peer_id: str, our_private_key: bytes, their_peer_id: str, relay_host: str, relay_port: int) -> NoiseSession:
    logger.info(f"connecting via relay to {their_peer_id[:12]}")

    reader, writer = await asyncio.open_connection(relay_host, relay_port)

    reg = json.dumps({"type": "register", "peer_id": our_peer_id}).encode()
    await write_frame(writer, reg)
    ack = json.loads(await read_frame(reader))
    if ack.get("type") != "ok":
        writer.close()
        raise ConnectionError(f"relay registeration failed {ack.get('reason')}")

    req = json.dumps({"type": "connect", "to": their_peer_id, "message_id": secrets.token_hex(8)}).encode()
    await write_frame(writer, req)

    resp = json.loads(await read_frame(reader))
    if resp.get("type") == "ok":
        logger.info(f"relay pipe working to {their_peer_id[:12]}, now noise handhskae")
        noise = await _do_handshake_initiator(reader, writer, our_private_key)
        return NoiseSession(noise=noise, reader=reader, writer=writer, our_peer_id=our_peer_id, remote_peer_id=their_peer_id, via_relay=True)

    elif resp.get("type") == "stored":
        logger.info(f"peer {their_peer_id[:12]} is offline , relay will store and forward")
        raise PeerOfflineError("Peer offline!, message stored to forward", writer)
    else:
        writer.close()
        raise ConnectionError(f"relay eror {resp.get('reason')}")


async def accept(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    our_peer_id: str,
    our_private_key: bytes,
    remote_peer_id: str = "unknown"
) -> NoiseSession:

    noise = await _do_handshake_responder(reader, writer, our_private_key)

    return NoiseSession(
        noise=noise,
        reader=reader,
        writer=writer,
        our_peer_id=our_peer_id,
        remote_peer_id=remote_peer_id,
        via_relay=False,
    )
