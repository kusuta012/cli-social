from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable
from noise.connection import NoiseConnection, Keypair

logger = logging.getLogger(__name__)


MessageHandler = Callable[[str, str, int], Awaitable[None]]
ReceiptHandler = Callable[[int], Awaitable[None]]


class PeerOfflineError(ConnectionError):
    def __init__(self, message, writer) -> None:
        super().__init__(message)
        self.writer = writer

async def encrypt_blob(our_peer_id: str, our_private_key: bytes, their_pubkey: bytes, content: str, client_message_id: str):
    noise = NoiseConnection.from_name(b"Noise_X_25519_ChaChaPoly_SHA256")
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

async def decrypt_blob(our_private_key: bytes, encrypted_blob: bytes) -> dict:
    noise = NoiseConnection.from_name(b"Noise_X_25519_ChaChaPoly_SHA256")
    noise.set_as_responder()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private_key)
    noise.start_handshake()
    
    decrypted_payload = noise.read_message(encrypted_blob)
    return json.loads(decrypted_payload)

