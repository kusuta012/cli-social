from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Input, ListView, ListItem, Label
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from cli_social.storage import Storage, DEFAULT_DB_PATH
from cli_social.p2p.transport import connect_via_relay, PeerOfflineError, encrypt_for_offline
from cli_social.p2p.daemon import Daemon
from cli_social.p2p.utils import write_frame


logger = logging.getLogger(__name__)

class ConversationItem(ListItem):
    def __init__(self, peer_id: str, username: str, unread: int = 0) -> None:
        super().__init__()
        self.peer_id = peer_id
        self.username = username
        self.unread = unread

    def compose(self) -> ComposeResult:
        unread_badge = f" ({self.unread})" if self.unread else ""
        name = self.username or self.peer_id[:16] + "..."
        yield Label(f" {name}{unread_badge}")


class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        width: 28;
        border-right: solid $primary;
    }
    #sidebar-title {
        padding: 1;
        text-style: bold;
        background: $boost;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(" Conversations", id="sidebar-title")
        yield ListView(id="conversation-list")

# the comment here is now oudated


class MessageBubble(Static):
    DEFAULT_CSS = """
    MessageBubble {
        padding: 0 1;
        margin: 0 0 1 0;
    }
    MessageBubble.outgoing {
        align: right middle;
        color: $success;
    }
    MessageBubble.incoming {
        align: left middle;
        color: $text;
    }
    """

    def __init__(self, content: str, sender: str, sent_at: str, is_outgoing: bool, delivered: bool = False, message_id: int = -1) -> None:
        super().__init__()
        self.msg_content = content
        self.sender = sender
        self.sent_at = sent_at
        self.is_outgoing = is_outgoing
        self.delivered = delivered
        self.message_id = message_id
        self.add_class("outgoing" if is_outgoing else "incoming")

    def compose(self) -> ComposeResult:
        time_str = self.sent_at[11:16]
        prefix = "you" if self.is_outgoing else self.sender[:10]
        tick = " [green]✓[/green]" if (self.is_outgoing and self.delivered) else (
            " [dim]·[/dim]" if self.is_outgoing else "")
        yield Label(f"[dim]{prefix} {time_str}[/dim]{tick}")
        yield Label(self.msg_content)

    def mark_delivered(self) -> None:
        self.delivered = True
        time_str = self.sent_at[11:16]
        self.query_one(Label).update(
            f"[dim]you {time_str}[/dim] [green]✓[/green]"
        )


class ChatPane(Vertical):
    DEFAULT_CSS = """
    ChatPane {
        width: 1fr;
    }

    #chat-header {
        padding: 1 2;
        background: $boost;
        text-style: bold;
        border-bottom: solid $primary;
    }
    #message-scroll {
        height: 1fr;
        padding: 1 2;
    }
    """

    current_peer_id: reactive[Optional[str]] = reactive(None)
    current_username: str = ""

    def compose(self) -> ComposeResult:
        yield Label("Select a conversation", id="chat-header")
        yield ScrollableContainer(id="message-scroll")

    async def load_messages(self, peer_id: str, username: str, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.current_peer_id = peer_id
        self.current_username = username
        self.query_one("#chat-header", Label).update(
            f"{username or peer_id[:16]}  [dim]●[/dim]"
        )

        scroll = self.query_one("#message-scroll", ScrollableContainer)
        await scroll.remove_children()

        async with await Storage.open(db_path) as s:
            messages = await s.get_messages(peer_id, limit=50)

        for msg in messages:
            bubble = MessageBubble(
                content=msg["content"],
                sender=username,
                sent_at=msg["sent_at"],
                is_outgoing=bool(msg["is_outgoing"]),
                delivered=bool(msg["delivered"]),
                message_id=msg["id"]
            )
            await scroll.mount(bubble)
        scroll.scroll_end(animate=False)

    async def append_message(self, content: str, sender: str, sent_at: str, is_outgoing: bool, delivered: bool = False, message_id: int = -1) -> MessageBubble:
        scroll = self.query_one("#message-scroll", ScrollableContainer)
        bubble = MessageBubble(
            content=content,
            sender=sender,
            sent_at=sent_at,
            is_outgoing=is_outgoing,
            delivered=delivered,
            message_id=message_id
        )
        await scroll.mount(bubble)
        scroll.scroll_end(animate=True)
        return bubble
    
    def set_status(self, status: str) -> None:
        dot = {"ok": "[green]●[/green]", "error": "[red]●[/red]",
               "connecting": "[yellow]●[/yellow]"}.get(status, "[dim]●[/dim]")
        header = self.query_one("#chat-header", Label)
        name = self.current_username or (self.current_peer_id or "")[:16]
        header.update(f"{name} {dot}")


class InputBar(Horizontal):
    DEFAULT_CSS = """
    InputBar {
        height: 3;
        border-top: solid $primary;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message... (Enter to send)", id="message-input")


class ChatModal(Vertical):
    DEFAULT_CSS = """
    ChatModal {
        width: 60;
        height: 10;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
        layer: overlay;
        offset: 20 8; 
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Start a new conversation")
        yield Label("Enter Peer ID: ")
        yield Input(placeholder="Peer ID...", id="chat-input")
        yield Label("", id="chat-modal-error")

    def _validate_peer_id(self, peer_id: str, own_peer_id: str) -> str | None:
        if len(peer_id) != 64 or not all(c in "0123456789abcdef" for c in peer_id):
            return "Invalid peer ID, must be 64 hex characters"
        if peer_id == own_peer_id:
            return "That's your own peer ID, FOOL"
        return None

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        peer_id = event.value.strip().lower()
        app: CLISocialApp = self.app  # type: ignore[assignment]
        err_label = self.query_one("#chat-modal-error", Label)
        error = self._validate_peer_id(peer_id, app.peer_id)
        if error:
            err_label.update(f"[red]{error}[/red]")
            return

        async with await Storage.open(app.db_path) as s:
            convos = await s.get_conversations()
        existing = [c for c in convos if c["peer_id"] == peer_id]
        if existing:
            err_label.update("[yellow]Conversation already exists[/yellow]")
            await app.on_existing_conversation(peer_id)
            self.remove()
            return

        await self.app.start_conversation(peer_id)  # type: ignore
        self.remove()

class CLISocialApp(App):
    TITLE = "cli-social"
    SUB_TITLE = "decentralized encrypted messaging"
    CSS = """
    Screen {
        layout: vertical;
    }
    
    #main-area {
        height: 1fr;
    }
    
    #sidebar-title {
        padding: 1;
        text-style: bold;
        background: $boost;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit",          "Quit"),
        ("ctrl+n", "new_chat",      "New Chat"),
        ("escape", "blur_input",    "Sidebar")
    ]

    def __init__(self, peer_id: str, private_key: bytes, username: str, listen_port: int = 9000, dht_port: int = 6969, bootstrap_nodes: list[tuple[str, int]] | None = None, db_path: Path = DEFAULT_DB_PATH, relay_host: str | None = None, relay_port: int = 9100) -> None:
        super().__init__()
        self.peer_id = peer_id
        self.private_key = private_key
        self.username = username
        self.listen_port = listen_port
        self.dht_port = dht_port
        self.bootstrap_nodes = bootstrap_nodes or []
        self.db_path = db_path
        self._daemon: Daemon | None = None
        self._daemon_task: asyncio.Task | None = None
        self._reannounce_task: asyncio.Task | None = None
        self._presence_task: asyncio.Task | None = None
        self.relay_host = relay_host
        self.relay_port = relay_port

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            yield Sidebar()
            yield ChatPane()
        yield InputBar()
        yield Footer()

    async def on_mount(self) -> None:
        # self.query_one(Header).title = f"Peer ID: {self.peer_id[:16]}" not sure about thsi
        await self._start_daemon()
        await self._load_conversations()
        self.set_focus(self.query_one("#conversation-list"))

    async def _start_daemon(self) -> None:
        try:
            self._daemon = Daemon(
                peer_id=self.peer_id,
                private_key=self.private_key,
                username=self.username,
                listen_port=self.listen_port,
                dht_port=self.dht_port,
                bootstrap_nodes=self.bootstrap_nodes,
                on_message=self.handle_incoming,
                db_path=self.db_path,
                relay_host=self.relay_host,
                relay_port=self.relay_port
            )
            await self._daemon.start()
            self._daemon_task = asyncio.create_task(self._daemon.run_forever(), name="daemon")
            if self._daemon._dht:
                self._reannounce_task = asyncio.create_task(self._daemon._dht.reannounce(username=self.username, noise_pubkey_hex=self._daemon.noise_pubkey_hex), name="dht_reannounce")
                self._presence_task = asyncio.create_task(self._presence_refresh(), name="presence_refresh")
            self.notify(f"Daemon up! | port {self.listen_port}", timeout=3)
        except Exception as e:
            self.notify(f"Daemon failed to start: {e}", severity="error", timeout=8)

    async def _load_conversations(self) -> None:
        async with await Storage.open(self.db_path) as s:
            convos = await s.get_conversations()

        lv = self.query_one("#conversation-list", ListView)
        await lv.clear()
        for c in convos:
            await lv.append(ConversationItem(
                peer_id=c["peer_id"],
                username=c.get("username") or "",
                unread=c.get("unread_count") or 0
            ))

    async def start_conversation(self, peer_id: str) -> None:
        async with await Storage.open(self.db_path) as s:
            await s.get_or_create_conversation(peer_id)
        await self._load_conversations()
    async def on_existing_conversation(self, peer_id: str) -> None:
        lv = self.query_one("#conversation-list", ListView)
        items = list(lv.query(ConversationItem))
        for i, item in enumerate(items):
            if item.peer_id == peer_id:
                lv.index = i
                chat = self.query_one(ChatPane)
                await chat.load_messages(item.peer_id, item.username, db_path=self.db_path)
                self.set_focus(self.query_one("#message-input"))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if not isinstance(item, ConversationItem):
            return
        chat = self.query_one(ChatPane)
        await chat.load_messages(item.peer_id, item.username, db_path=self.db_path)
        async with await Storage.open(self.db_path) as s:
            convo_id = await s.get_or_create_conversation(item.peer_id)
            await s.mark_read(convo_id)
            contact = await s.get_contact(item.peer_id)
        
        if contact and contact.get("fingerprint"):
            fp = contact["fingerprint"]
            chat.query_one("#chat-header", Label).update(f"{item.username or item.peer_id[:16]} [dim]({fp[:8]}...{fp[-8:]})[/dim] F")
                    
        if self._daemon and self._daemon._dht:
            online = await self._daemon._dht.is_online(item.peer_id)
            chat.set_status("ok" if online else "error")
        self.set_focus(self.query_one("#message-input"))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "message-input":
            return
        content = event.value.strip()
        if not content:
            return

        chat = self.query_one(ChatPane)
        if not chat.current_peer_id:
            self.notify("Select a conversation first!")
            return

        event.input.value = ""
        await self._send_message(chat.current_peer_id, content)

    async def _send_message(self, peer_id: str, content: str) -> None:
        chat = self.query_one(ChatPane)
        chat.set_status("connecting")
        
        MAX_RETRIES = 3
        RETRY_DELAY = 2
        
        client_message_id = str(uuid.uuid4())
        session = None
        delivered = False
        
        for attempt in range(MAX_RETRIES):
            try:
                assert self._daemon, "Daemon is not running"
                active_relay_host = self._daemon.relay_host
                active_relay_port = self._daemon.relay_port
            
                if not active_relay_host:
                    self.notify("still discovering relay network... pls wait", severity="warning")
                    chat.set_status("error")
                    return
            
                session = await connect_via_relay(our_peer_id=self.peer_id, our_private_key=self.private_key, their_peer_id=peer_id, relay_host=active_relay_host, relay_port=active_relay_port)
                logger.debug(f"relay connection to {peer_id[:12]} worked via {active_relay_host}:{active_relay_port}")
                await session.send(content, client_message_id)
                chat.set_status("ok")
                delivered = True
                
                async with await Storage.open(self.db_path) as s:
                    await s.update_contact_fingerprint(peer_id, session.fingerprint)
                break
                            
            except PeerOfflineError as e:
                self.notify(f"Peer is offline, storing to forward later")
                try:
                    assert self._daemon._dht, "DHT not running" # type: ignore
                    peer_info = await self._daemon._dht.lookup(peer_id) # type: ignore
                    if not peer_info or not peer_info.noise_pubkey_hex:
                        raise ValueError("could not find peer's public key in DHT for offline messaging")
                    their_pubkey = bytes.fromhex(peer_info.noise_pubkey_hex)
                    encrypt_blob = await encrypt_for_offline(self.peer_id, self.private_key, their_pubkey, content, client_message_id)
                    await write_frame(e.writer, encrypt_blob)
                    e.writer.close()
                    
                    self.notify("msg stored for later", severity="information")
                    chat.set_status("ok")
                    delivered = False
                    break        
                except Exception as store_err:
                    self.notify(f"failed to store msg {store_err}", severity="error")
                    chat.set_status("error")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    return
            
            except Exception as e:
                self.notify(f"failed to send (attempt {attempt + 1}/{MAX_RETRIES}, {e})", severity="warning")
                chat.set_status("error")
                if attempt < MAX_RETRIES -1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    self.notify(f"failed to send {MAX_RETRIES} attempts {e}", severity="error")
                    return
            finally:
                if session:
                    session.close()
        
        now = datetime.now(timezone.utc).isoformat()
        async with await Storage.open(self.db_path) as s:
            message_id = await s.save_message(
                peer_id=peer_id,
                sender_peer_id=self.peer_id,
                content=content,
                is_outgoing=True,
                delivered=delivered
            )
        bub = await chat.append_message(content, "you", now, is_outgoing=True, message_id=message_id, delivered=delivered)
        if delivered:
            bub.mark_delivered()    


    async def handle_incoming(self, peer_id: str, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        chat = self.query_one(ChatPane)
        if chat.current_peer_id == peer_id:
            username = peer_id[:10]
            for item in self.query(ConversationItem):
                if item.peer_id == peer_id:
                    username = item.username or peer_id[:10]
                    break
            await chat.append_message(content, username, now, is_outgoing=False)
        else:
            self.notify(f"New message from {peer_id[:12]}....")
        await self._load_conversations()

    async def _presence_refresh(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                chat = self.query_one(ChatPane)
                if chat.current_peer_id and self._daemon and self._daemon._dht:
                    online = await self._daemon._dht.is_online(chat.current_peer_id)
                    chat.set_status("ok" if online else "error")
            except Exception:
                pass
            

    def action_quit(self) -> None:
        if self._daemon_task:
            self._daemon_task.cancel()
        if self._reannounce_task:
            self._reannounce_task.cancel()
        if self._presence_task:
            self._presence_task.cancel()
        self.exit()

    def action_new_chat(self) -> None:
        self.mount(ChatModal())
        self.query_one("#chat-input", Input).focus()

    def action_blur_input(self) -> None:
        self.set_focus(self.query_one("#conversation-list"))


def run(peer_id: str = "", private_key: bytes = b"", username: str = "", listen_port: int = 9000, dht_port: int = 6969, bootstrap_nodes: list[tuple[str, int]] | None = None, db_path: Path = DEFAULT_DB_PATH, relay_host: str | None = None, relay_port: int = 9100) -> None:
    CLISocialApp(peer_id=peer_id, private_key=private_key, username=username, listen_port=listen_port,
                 dht_port=dht_port, bootstrap_nodes=bootstrap_nodes or [], db_path=db_path, relay_host=relay_host, relay_port=relay_port).run()
