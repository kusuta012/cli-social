from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional
from kademlia.network import Server

logger = logging.getLogger(__name__)

DEFAULT_DHT_PORT = 6969
BOOTSTRAP_NODES: list[tuple[str, int]] = [
    # will keep this as it is for now , need to set a reminder for this ahh
]

@dataclass
class PeerInfo:
    peer_id: str
    host: str
    port: int
    username: str = ""
    
    def to_json(self) -> str:
        return json.dumps({
            "peer_id": self.peer_id,
            "host": self.host,
            "port": self.port,
            "username": self.username
        })
    @classmethod
    def from_json(cls, data:str) -> "PeerInfo":
        d = json.loads(data)
        return cls(
            peer_id=d["peer_id"],
            host=d["host"],
            port=d["port"],
            username=d.get("username", "")   
        )

class DHTNode:
    def __init__(
        self,
        peer_id: str,
        host: str = "0.0.0.0",
        port: int = DEFAULT_DHT_PORT,
        bootstrap_nodes: list[tuple[str, int]] = BOOTSTRAP_NODES,
    ):
        self.peer_id = peer_id
        self.host = host
        self.port = port
        self.bootstrap_nodes = bootstrap_nodes
        self._server = Server()
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
    
    async def reannounce(self, username: str = "", listen_port: int = 9000, host: str = "127.0.0.1") -> None:
        while self._started:
            await self.announce(username=username, listen_port=listen_port, host=host)
            await asyncio.sleep(15)
    
    async def stop(self) -> None:
        if self._started:
            self._server.stop()
            self._started = False
            logger.info("DHT node stopped")
            
    async def announce(self, username: str = "", listen_port: int = 9000, host: str = "127.0.0.1") -> None:
        info = PeerInfo(
            peer_id=self.peer_id,
            host=host,
            port=listen_port,
            username=username
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

        
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, *_):
        await self.stop()
