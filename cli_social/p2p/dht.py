from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from kademlia.network import Server

logger = logging.getLogger(__name__)

DEFAULT_DHT_PORT = 6969


@dataclass
class PeerInfo:
    peer_id: str
    username: str = ""
    last_seen: str = ""
    noise_pubkey_hex: str = ""
    
    def to_json(self) -> str:
        return json.dumps({
            "peer_id": self.peer_id,
            "username": self.username,
            "last_seen": self.last_seen,
            "noise_pubkey_hex": self.noise_pubkey_hex
        })
    @classmethod
    def from_json(cls, data:str) -> "PeerInfo":
        d = json.loads(data)
        return cls(
            peer_id=d["peer_id"],
            username=d.get("username", ""),
            last_seen=d.get("last_seen", ""),
            noise_pubkey_hex=d.get("noise_pubkey_hex", "")
        )

class DHTNode:
    def __init__(
        self,
        peer_id: str,
        host: str = "0.0.0.0",
        port: int = DEFAULT_DHT_PORT,
        bootstrap_nodes: list[tuple[str, int]] | None = None
    ):
        self.peer_id = peer_id
        self.host = host
        self.port = port
        self.bootstrap_nodes = bootstrap_nodes or []
        self._server = Server(ksize=20, alpha=3)
        self._started = False
    
    async def start(self) -> None:
        await self._server.listen(self.port)
        self._started = True
        logger.info(f"TNT?? nah DHT node listening on port {self.port}")
        
        if self.bootstrap_nodes:
            await self._server.bootstrap(self.bootstrap_nodes)
            await asyncio.sleep(2)
            logger.info(f"bootsrapped with {self.bootstrap_nodes}")
        else:
            logger.warning("No nodes configured | in local mode")
    
    async def reannounce(self, username: str = "", noise_pubkey_hex: str = "") -> None:
        while self._started:
            await self.announce(username=username, noise_pubkey_hex=noise_pubkey_hex)
            await asyncio.sleep(60)
    
    async def stop(self) -> None:
        if self._started:
            self._server.stop()
            self._started = False
            logger.info("DHT node stopped")
            
    async def announce(self, username: str = "", noise_pubkey_hex: str = "") -> None:
        info = PeerInfo(
            peer_id=self.peer_id,
            username=username,
            last_seen=datetime.now(timezone.utc).isoformat(),
            noise_pubkey_hex=noise_pubkey_hex
        )
        await self._server.set(self.peer_id, info.to_json())
        logger.info(f"Announced self to DHT {self.peer_id[:12]}")
        
    async def lookup(self, peer_id: str) -> Optional[PeerInfo]:
        result = await self._server.get(peer_id)
        if result is None:
            logger.debug(f"peer {peer_id[:12]} not found")
            return None
        try:
            return PeerInfo.from_json(result)
        except Exception as e:
            logger.error(f"parsing failed for peer info {peer_id[:12]} {e}")
            return None

    async def is_online(self, peer_id: str, threshold_seconds: int = 45) -> bool:
        info = await self.lookup(peer_id)
        if not info or not info.last_seen:
            return False
        last_seen = datetime.fromisoformat(info.last_seen)
        age = (datetime.now(timezone.utc) - last_seen).total_seconds()
        return age < threshold_seconds
    
    async def get_value(self, key: str) -> str | None:
        return await self._server.get(key)
    
    async def set_value(self, key: str, value: str) -> None:
        await self._server.set(key, value)
        
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, *_):
        await self.stop()
