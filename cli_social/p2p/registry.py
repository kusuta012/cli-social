import json
import urllib.error
import urllib.request
import asyncio
import logging
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)

TRUSTED_PUBKEYS = ["60c01e07546f068b051fe54a2f0b0e34a67a129b6e9eccb235923706734def9c"]

def vrfy_registry_sign(public_key_hex: str, data: dict, sign_hex: str) -> bool:
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        sign_bytes = bytes.fromhex(sign_hex)
        
        payload_to_vrfy = {k: v for k, v in data.items() if k != "signatures"}
        
        message = json.dumps(payload_to_vrfy, sort_keys=True).encode('utf-8')
        
        public_key.verify(sign_bytes, message)
        return True
    except (InvalidSignature, ValueError) as e:
        logger.debug(f"Signature verification failed: {e}")
        return False

async def fetch_and_vrfy_registry(pointer_url: str, accept_community: bool = False) -> list[dict]:
    def _fetch():
        req = urllib.request.Request(pointer_url, headers={'User-Agent': 'cli-social-node/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
        
    try:
        registry_data = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.error(f"failed to fetch registry from {pointer_url}: {e}")
        raise
    
    signatures = registry_data.get("signatures", {})
    valid_sigs = 0
    
    for pubkey, sig in signatures.items():
        if pubkey in TRUSTED_PUBKEYS or accept_community:
            if vrfy_registry_sign(pubkey, registry_data, sig):
                valid_sigs+=1
    
    if valid_sigs == 0 and not accept_community:
        raise ValueError("Registery rejected, no valid trusted signatures found")


    return registry_data.get("relays",[])
