from __future__ import annotations
import asyncio
import logging
from typing import Callable, Awaitable
from cli_social.p2p.transport import accept, NoiseSession
from cli_social.p2p.dht import DHTNode
from cli_social.storage import Storage

logger = logging.getLogger(__name__)


MessageCallback = Callable[[str, str], Awaitable[None]]


class Daemon:
    def __init__(
        self,
        peer_id: str,
        private_key: bytes,
        username: str = "",
        listen_port: int = 9000,
        dht_port: int = 6969,
        bootstrap_nodes: list[tuple[str, int]] = [],
        on_message: MessageCallback | None = None
    ):
        self.peer_id = peer_id
        self.private_key = private_key
        self.username = username
        self.listen_port = listen_port
        self.dht_port = dht_port
        self.bootstrap_nodes = bootstrap_nodes
        self.on_message = on_message
        self._server: asyncio.Server | None = None
        self._dht: DHTNode | None = None
        self._storage: Storage | None = None
        self._running = False

    # coding this at 1am , I need caffeine !!
    async def start(self) -> None:
        self._storage = await Storage.open()
        self._dht = DHTNode(
            peer_id=self.peer_id,
            port=self.dht_port,
            bootstrap_nodes=self.bootstrap_nodes
        )
        await self._dht.start()
        await self._dht.announce(
            username=self.username,
            listen_port=self.listen_port
        )
        
        self._server = await asyncio.start_server(
            self._handle_connection,
            "0.0.0.0",
            self.listen_port
        )
        self._running = True
        logger.info(f"Daemon listening on {self.listen_port}")
        logger.info(f"peer id {self.peer_id[:16]}.....{self.peer_id[-8:]}")
        
    async def run_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("not ur fault , should call start()")    
        async with self._server:
            await self._server.serve_forever()
    
    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._dht:
            await self._dht.stop()
        if self._storage:
            await self._storage.close()
        logger.info("dameon stopped")
            
            
    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ) -> None:
        peer_addr = writer.get_extra_info("peername")
        logger.info(f"incoming from {peer_addr}")
        
        try:
            session: NoiseSession = await accept(
                reader=reader,
                writer=writer,
                our_peer_id=self.peer_id,
                our_private_key=self.private_key
            )
            logger.info(f"handhshake completed {peer_addr}")
            
            async def _msg_handler(peer_id: str, content: str, message_id: int) -> None:
                await self._on_message(peer_id, content, session)
            
            await session.listen(_msg_handler, on_receipt=None)
            
        except Exception as e:
            logger.error(f"connection from {peer_addr} failed {e}")
        finally:
            writer.close()
            
    async def _on_message(self, peer_id: str, content: str, session: NoiseSession) -> None:
        message_id = -1
        if self._storage:
            message_id = await self._storage.save_message(
                peer_id=peer_id,
                sender_peer_id=peer_id,
                content=content,
                is_outgoing=False
            )
        
        try:
            await session.send_receipt(message_id)
            logger.debug(f"sent receipt {message_id} to {peer_id[:12]}")
        except Exception as e:
            logger.warning(f"failed to send receipt: {e}")
            
        if self.on_message:
            await self.on_message(peer_id, content)