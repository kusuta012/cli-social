from __future__ import annotations
import json
import logging
import hashlib
from datetime import datetime, timezone
from typing import Callable, Awaitable
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from noise.connection import NoiseConnection, Keypair

logger = logging.getLogger(__name__)


MessageHandler = Callable[[str, str, int], Awaitable[None]]
ReceiptHandler = Callable[[int], Awaitable[None]]


class PeerOfflineError(ConnectionError):
    def __init__(self, message, writer) -> None:
        super().__init__(message)
        self.writer = writer

async def encrypt_blob(our_peer_id: str, our_private_key: bytes, their_pubkey: bytes, content: str, client_message_id: str, ed25519_private_key=None):
    noise = NoiseConnection.from_name(b"Noise_X_25519_ChaChaPoly_SHA256")
    noise.set_as_initiator()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private_key)
    noise.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, their_pubkey)
    noise.start_handshake()
    inner = {
            "type": "message",
            "peer_id": our_peer_id,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_message_id": client_message_id
    }
    if ed25519_private_key:
        inner_bytes = json.dumps(inner, separators=(',', ':'), sort_keys=True).encode()
        inner["signature"] = ed25519_private_key.sign(inner_bytes).hex()
        inner["ed25519_pubkey"] = ed25519_private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    payload = json.dumps(inner).encode()
    encrypted_message = noise.write_message(payload=payload)
    return bytes(encrypted_message)

async def decrypt_blob(our_private_key: bytes, encrypted_blob: bytes) -> dict:
    noise = NoiseConnection.from_name(b"Noise_X_25519_ChaChaPoly_SHA256")
    noise.set_as_responder()
    noise.set_keypair_from_private_bytes(Keypair.STATIC, our_private_key)
    noise.start_handshake()
    
    decrypted_payload = noise.read_message(encrypted_blob)
    data = json.loads(decrypted_payload)
    if "signature" in data and "ed25519_pubkey" in data:
        pubkey_hex = data.pop("ed25519_pubkey")
        signature_hex = data.pop("signature")
        pub_bytes = bytes.fromhex(pubkey_hex)
        expected_peer_id = hashlib.sha256(pub_bytes).hexdigest()
        if expected_peer_id != data.get("peer_id"):
            raise ValueError("sender pubkey does not match claimed peer id (SPOOFING ALERT!)")
        ed_pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        verify_data = json.dumps({k: v for k, v in data.items()}, separators=(',', ':'), sort_keys=True).encode()
        ed_pub.verify(bytes.fromhex(signature_hex), verify_data)
        data["verified"] = True
    else:
        raise ValueError("message has no signature, rejecting unsigned paayload")
    return data

