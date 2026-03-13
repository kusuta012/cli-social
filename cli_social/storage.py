from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import aiosqlite

DEFAULT_DB_PATH = Path.home() / ".cli-social" / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    peer_id TEXT PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    public_key TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id TEXT NOT NULL UNIQUE,
    last_message_at TEXT,
    unread_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    sender_peer_id TEXT NOT NULL,
    content TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0
);
"""

def db_path_for(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir / "data.db"
    return DEFAULT_DB_PATH

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class Storage:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db
        
    @classmethod
    async def open(cls, db_path: Path = DEFAULT_DB_PATH) -> "Storage":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.commit()
        return cls(db)
    
    async def close(self):
        await self._db.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *_):
        await self.close()
        
    async def add_contact(
        self,
        peer_id: str,
        username: str = "",
        public_key: str = "",
        host: str = "",
        port: int = 0
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO contacts (peer_id, username, public_key, host, port, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                username = excluded.username,
                public_key = excluded.public_key,
                host = excluded.host,
                port = excluded.port
            """,
            (peer_id, username, public_key, host, port, _now())
        )
        await self._db.commit()
        
    async def get_contacts(self) -> list[dict]:
        async with self._db.execute(
            "SELECT peer_id, username, public_key, host, port, added_at FROM contacts ORDER BY username"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_contact(self, peer_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT peer_id, username, public_key, added_at FROM contacts WHERE peer_id = ?",
            (peer_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    async def get_contact_pubkey(self, peer_id: str) -> str | None:
        async with self._db.execute("SELECT public_key FROM contacts WHERE peer_id = ?", (peer_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return row["public_key"] or None
    
    async def update_contact_pubkey(self, peer_id: str, pubkey_hex: str) -> None:
        await self._db.execute("UPDATE contacts SET public_key = ? WHERE peer_id = ?", (pubkey_hex, peer_id))
        await self._db.commit()
        
    async def get_or_create_conversation(self, peer_id: str) -> int:
        async with self._db.execute(
            "SELECT id FROM conversations WHERE peer_id = ?", (peer_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row["id"]
        
        cursor = await self._db.execute(
            "INSERT INTO conversations (peer_id, last_message_at, unread_count) VALUES (?, ?, 0)",
            (peer_id, _now()),
        )
        await self._db.commit()
        assert cursor.lastrowid is not None, "insert failed no lastrowid returned"
        return cursor.lastrowid
    
    async def get_conversations(self) -> list[dict]:
        async with self._db.execute(
            """
            SELECT c.id, c.peer_id, c.last_message_at, c.unread_count, ct.username
            FROM conversations c
            LEFT JOIN contacts ct ON ct.peer_id = c.peer_id
            ORDER BY c.last_message_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        
    async def mark_read(self, conversation_id: int) -> None:
        await self._db.execute(
            "UPDATE conversations SET unread_count = 0 WHERE id = ?",
            (conversation_id,),
        )
        await self._db.commit()    
        
        
    async def save_message(
        self,
        peer_id: str,
        sender_peer_id: str,
        content: str,
        is_outgoing: bool = False,
    ) -> int:
        conv_id = await self.get_or_create_conversation(peer_id)
        now = _now()
        cursor = await self._db.execute(
            """
            INSERT INTO messages (conversation_id, sender_peer_id, content, sent_at, is_outgoing)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conv_id, sender_peer_id, content, now, int(is_outgoing)),
        )
        
        if not is_outgoing:
            await self._db.execute(
                """
                UPDATE conversations
                SET last_message_at = ?,
                    unread_count = unread_count + 1
                WHERE id = ?
                """,
                (now, conv_id),
            )
        else:
            await self._db.execute(
                "UPDATE conversations SET last_message_at = ? WHERE id = ?",
                (now, conv_id),
            )
        await self._db.commit()
        assert cursor.lastrowid is not None, "insert failed no lastrowid returned"
        return cursor.lastrowid

       # yo phone linging.. yo phone linging big boy come pick up ur phone, why you no pick up ur phone ... yo phone linging 
       # ignore these random comments ahh
       
       #tung tung tung tung tung tung tung sahur :O
    async def mark_delivered(self, message_id: int) -> None:
        await self._db.execute(
            "UPDATE messages SET delivered = 1 WHERE id = ?", (message_id,)
        )
        await self._db.commit()

    async def get_messages(
        self,
        peer_id: str,
        limit: int = 50 ,
    ) -> list[dict]:
        async with self._db.execute(
            """
            SELECT m.id, m.sender_peer_id, m.content, m.sent_at, m.is_outgoing, m.delivered
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.peer_id = ?
            ORDER BY m.sent_at DESC
            LIMIT ?
            """,
            (peer_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in reversed(list(rows))]