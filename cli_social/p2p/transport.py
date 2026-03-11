from __future__ import annotations
import asyncio
import json
import logging
import os
import struct
from datetime import datetime, timezone
from typing import Callable, Awaitable
from noise.connection import NoiseConnection, Keypair
from noise.backends.default import diffie_hellmans as DH

logger = logging.getLogger(__name__)

MAX_FRAME_SIZE = 64 * 1024
MessageHandler = Callable[[str, str], Awaitable[None]]


def generate_noise_keypair() -> tuple[bytes, bytes]:
    keypair = DH.ED25519().generate_keypair()
    return keypair.public_bytes, keypair.private.private_bytes_raw()


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"frame is large: {length} bytes")
    return await reader.readexactly(length)

async def _write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()
    
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
    await _write_frame(writer, bytes(msg1))
    
    msg2 = await _read_frame(reader)
    noise.read_message(msg2)
    
    msg3 = noise.write_message()
    await _write_frame(writer, bytes(msg3))
    
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
    
    msg1 = await _read_frame(reader)
    noise.read_message(msg1)
    
    msg2 = noise.write_message()
    await _write_frame(writer, bytes(msg2))
    
    msg3 = await _read_frame(reader)
    noise.read_message(msg3)
    
    assert noise.handshake_finished, "Hanshake did not finish :("
    logger.debug("Noise handshake finished (resp)")
    return noise

class NoiseSession:
    def __init__(
        self,
        noise: NoiseConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        our_peer_id: str,
        remote_peer_id: str
    ):
        self._noise = noise
        self._reader = reader
        self._writer = writer
        self.our_peer_id = our_peer_id
        self.remote_peer_id = remote_peer_id
        
    async def send(self, content: str) -> None:
        payload = json.dumps({
            "peer_id": self.our_peer_id,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }).encode()
        encrypted = self._noise.encrypt(payload)
        await _write_frame(self._writer, encrypted)
        logger.debug(f"sent to {self.remote_peer_id[:12]}")
        
    async def receive(self) -> dict:
        frame = await _read_frame(self._reader)
        plaintext = self._noise.decrypt(frame)
        return json.loads(plaintext)
    
    async def listen(self, on_message: MessageHandler) -> None:
        try:
            while True:
                msg = await self.receive()
                await on_message(msg["peer_id"], msg["content"])
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
        remote_peer_id=their_peer_id
    )
    
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
        remote_peer_id=remote_peer_id
    )