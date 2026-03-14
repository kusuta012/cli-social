from __future__ import annotations
import hashlib
import json
import os
import secrets
from pathlib import Path
from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import (
    Encoding, 
    NoEncryption,
    PrivateFormat,
    PublicFormat
)

DEFAULT_KEY_FILE = Path.home() / ".cli-social" / "identity.enc"
ARGON2_TIME_COST = 3
ARGON2_MEM_COST = 65536
ARGON2_PARALLELISM = 2
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16
AES_NONCE_LEN = 12


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hash_secret_raw(
        secret=passphrase.encode(),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEM_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID,
    )
    
def _peer_id_from_public_key(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()

def _key_file_for(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir / "identity.enc"
    return DEFAULT_KEY_FILE

def generate_identity(
    passphrase: str,
    username: str = "",
    key_file: Path = DEFAULT_KEY_FILE
) -> str:

    if key_file.exists():
        raise FileExistsError(f"Identity file already exists: {key_file}")
    
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    
    noise_private = X25519PrivateKey.generate()
    noise_private_bytes = noise_private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    noise_public_bytes = noise_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    
    salt = secrets.token_bytes(ARGON2_SALT_LEN)
    aes_key = _derive_key(passphrase, salt)
    nonce = secrets.token_bytes(AES_NONCE_LEN)
    aesgcm = AESGCM(aes_key)
    ciphertext= aesgcm.encrypt(nonce, private_bytes, None)
    noise_nonce = secrets.token_bytes(AES_NONCE_LEN)
    noise_ciphertext = aesgcm.encrypt(noise_nonce, noise_private_bytes, None)
    
    peer_id = _peer_id_from_public_key(public_key)
    payload = {
        "version": 1,
        "argon2_salt": salt.hex(),
        "aes_nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "noise_nonce": noise_nonce.hex(),
        "noise_ciphertext": noise_ciphertext.hex(),
        "noise_public_key": noise_public_bytes.hex(),
        "peer_id": peer_id,
        "username": username,
    }
    
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(json.dumps(payload, indent=2))
    
    try:
        os.chmod(key_file, 0o600)
    except NotImplementedError:
        pass    # I hate you windows user :)
    
    return peer_id

def load_identity(
    passphrase: str,
    key_file: Path = DEFAULT_KEY_FILE
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey, str, str, bytes]:
    
    if not key_file.exists():
        raise FileNotFoundError(f"No identity found at {key_file}")
    
    payload = json.loads(key_file.read_text())
    if payload.get("version") !=1:
        raise ValueError(f"Unsupported identity file version: {payload.get('version')}")
    
    salt = bytes.fromhex(payload["argon2_salt"])
    nonce = bytes.fromhex(payload["aes_nonce"])
    ciphertext = bytes.fromhex(payload["ciphertext"])
    aes_key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(aes_key)
    
    try:
        private_bytes = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("Decryption failed!! wrong passphrase or corrupt key file") from exc
    
    noise_nonce = bytes.fromhex(payload["noise_nonce"])
    noise_ciphertext = bytes.fromhex(payload["noise_ciphertext"])
    noise_private_bytes = aesgcm.decrypt(noise_nonce, noise_ciphertext, None)
    
    
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    public_key = private_key.public_key()
    peer_id = _peer_id_from_public_key(public_key)
    username = payload.get("username", "")
    
    return private_key, public_key, peer_id, username, noise_private_bytes

def identity_exists(key_file: Path = DEFAULT_KEY_FILE) -> bool:
    return key_file.exists()

def sign_registry_doc(private_key: Ed25519PrivateKey, registry_data: dict) -> dict:
    payload = {k: v for k, v in registry_data.items() if k != "signatures"}
    message = json.dumps(payload, sort_keys=True).encode('utf-8')
    signature = private_key.sign(message)
    pub_hex = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    registry_data["signatures"] = { pub_hex: signature.hex() }
    return registry_data