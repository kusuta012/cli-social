from __future__ import annotations
import asyncio
import logging
import uuid
import json
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Input, ListView, ListItem, Label
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.screen import ModalScreen
from cli_social.storage import Storage, DEFAULT_DB_PATH
from cli_social.p2p.daemon import Daemon



logger = logging.getLogger(__name__)


class ConversationItem(ListItem):
    def __init__(self, peer_id: str, username: str, unread: int = 0) -> None:
        super().__init__()
        self.peer_id = peer_id
        self.username = username
        self.unread = unread

    def compose(self) -> ComposeResult:
        unread_badge = f" [bold red]({self.unread})[/bold red]" if self.unread else ""
        name = self.username or self.peer_id[:12] + "..."
        yield Label(f" {name}{unread_badge}", classes="convo-label")


class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        width: 30;
        border-right: vkey $primary;
        background: $panel;
    }
    #sidebar-title {
        padding: 1 2;
        text-style: bold;
        background: $boost;
        width: 100%;
        color: $text;
        border-bottom: solid $primary;
    }
    .convo-label {
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(" Conversations", id="sidebar-title")
        yield ListView(id="conversation-list")
class MessageBubble(Static):
    DEFAULT_CSS = """
    MessageBubble {
        padding: 1 4;
        margin: 0;
        width: 100%;
        layout: vertical;
    }
    MessageBubble.outgoing {
        color: $success;
        content-align: right top;
    }
    MessageBubble.incoming {
        content-align: left top;
        color: $text;
    }
    .msg-meta {
        color: $text-muted;
        text-style: bold italic;
        margin-bottom: 1;
    }
    MessageBubble Label {
        width: auto;
    }
    """

    def __init__(
        self,
        content: str,
        sender: str,
        sent_at: str,
        is_outgoing: bool,
        delivered: bool = False,
        message_id: int = -1,
        client_message_id: str = "",
        **kwargs
    ):
        super().__init__()
        self.msg_content = content
        self.sender = sender
        self.sent_at = sent_at
        self.is_outgoing = is_outgoing
        self.delivered = delivered
        self.message_id = message_id
        self.client_message_id = client_message_id
        self.status = "pending"
        self.add_class("outgoing" if is_outgoing else "incoming")
    
    def compose(self) -> ComposeResult:
        time_str = self.sent_at[11:16]
        prefix = "You" if self.is_outgoing else self.sender[:12]
        icon = ""
        if self.is_outgoing:
            icon = {
            "pending": "[red]●[/red]",
            "relayed": "[yellow]●[/yellow]",
            "delivered": "[green]✓[/green]",
            "failed": "[bold red]![/bold red]"
        }.get(self.status, " ")
        yield Label(f"{prefix} {time_str} {icon}", classes="msg-meta")
        yield Label(self.msg_content)

    def update_status(self, status: str) -> None:
        self.status = status
        icon = {
            "pending": "[red]●[/red]",
            "relayed": "[yellow]●[/yellow]",
            "delivered": "[green]✓[/green]",
            "failed": "[bold red]![/bold red]"
        }.get(status, " ")
        time_str = self.sent_at[11:16]
        prefix = "You" if self.is_outgoing else self.sender[:12]
        meta = self.query_one(".msg-meta", Label)
        meta.update(f"{prefix} {time_str} {icon}")


class ChatPane(Vertical):
    DEFAULT_CSS = """
    ChatPane {
        width: 1fr;
        background: $surface;
    }

    #chat-header {
        padding: 1 2;
        background: $boost;
        text-style: bold;
        border-bottom: solid $primary;
        width: 100%;
        content-align: left middle;
    }
    #message-scroll {
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    current_peer_id: reactive[Optional[str]] = reactive(None)
    current_username: str = ""

    def compose(self) -> ComposeResult:
        yield Label("Select a conversation", id="chat-header")
        yield ScrollableContainer(id="message-scroll")

    async def load_messages(
        self, peer_id: str, username: str, db_path: Path = DEFAULT_DB_PATH
    ) -> None:
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
                message_id=msg["id"],
            )
            await scroll.mount(bubble)
        scroll.scroll_end(animate=False)

    async def append_message(
        self,
        content: str,
        sender: str,
        sent_at: str,
        is_outgoing: bool,
        delivered: bool = False,
        message_id: int = -1,
        client_message_id: str = ""
    ) -> MessageBubble:
        scroll = self.query_one("#message-scroll", ScrollableContainer)
        bubble = MessageBubble(
            content=content,
            sender=sender,
            sent_at=sent_at,
            is_outgoing=is_outgoing,
            delivered=delivered,
            message_id=message_id,
            client_message_id=client_message_id
        )
        await scroll.mount(bubble)
        scroll.scroll_end(animate=True)
        return bubble

    def set_status(self, status: str) -> None:
        dot = {
            "ok": "[green]●[/green]",
            "error": "[red]●[/red]",
            "connecting": "[yellow]●[/yellow]",
        }.get(status, "[dim]●[/dim]")
        header = self.query_one("#chat-header", Label)
        name = self.current_username or (self.current_peer_id or "")[:16]
        header.update(f"{name} {dot}")


class InputBar(Horizontal):
    DEFAULT_CSS = """
    InputBar {
        height: 3;
        dock: bottom;
        border-top: solid $primary;
        background: $boost;
    }
    #messages-input {
        width: 1fr;
        border: none;
        background: transparent;
    }
    #messages-input:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message... (Enter to send)", id="message-input")


class ChatModal(ModalScreen):
    DEFAULT_CSS = """
    ChatModal {
        align: center middle;
        background: $background 50%;
    }
    #modal-dialog {
        width: 60;
        height: auto;
        padding: 2 4;
        background: $surface;
        border: thick $primary;
    }
    #chat-modal-error {
        margin-top: 1;
        content-align: center middle;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
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
        app: CLISocialApp = self.app
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
            self.dismiss()
            return

        await self.app.start_conversation(peer_id)
        self.dismiss()

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
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+n", "new_chat", "New Chat"),
        ("escape", "blur_input", "Sidebar"),
    ]

    def __init__(
        self,
        peer_id: str,
        private_key: bytes,
        username: str,
        listen_port: int = 63012,
        dht_port: int = 6969,
        bootstrap_nodes: list[tuple[str, int]] | None = None,
        db_path: Path = DEFAULT_DB_PATH,
        relay_host: str | None = None,
        relay_port: int = 9100,
    ) -> None:
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
        self._presence_task: asyncio.Task | None = None
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.state_path = db_path.parent / "state.json"
        self.cached_relay = None
        if self.state_path.exists():
            try:
                self.cached_relay = json.loads(self.state_path.read_text())
                logger.info(f"loaded cache relay {self.cached_relay}")
            except Exception:
                logger.warning("could not load cached relay")
                pass

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            yield Sidebar()
            yield ChatPane()
        yield InputBar()
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = f"Peer ID: {self.peer_id[:12]}"
        self.run_worker(self._start_daemon)
        await self._load_conversations()
        self.set_focus(self.query_one("#conversation-list"))

    async def _start_daemon(self) -> None:
        self.notify("Starting daemon, this may take a while", title="System", severity="information", timeout=5)
        self.query_one(ChatPane).set_status("connecting")
        try:
            self._daemon = Daemon(
                peer_id=self.peer_id,
                private_key=self.private_key,
                username=self.username,
                bootstrap_nodes=self.bootstrap_nodes,
                on_message=self.handle_incoming,
                status_callback=self.handle_status_update,
                db_path=self.db_path,
                relay_host=self.relay_host,
                relay_port=self.relay_port,
                cached_relay=self.cached_relay,
            )
            await self._daemon.start()
            self._daemon_task = asyncio.create_task(
                self._daemon.run_forever(), name="daemon"
            )
            if self._daemon._dht:
                self._presence_task = asyncio.create_task(
                    self._presence_refresh(), name="presence_refresh"
                )
            self.notify (f"Daemon up! | port {self.listen_port}", severity ="information", timeout=3)
        except Exception as e:
            self.notify(f"Daemon failed to start: {e}", severity="error", timeout=8)
            self.query_one(ChatPane).set_status("error")

    async def _load_conversations(self) -> None:
        async with await Storage.open(self.db_path) as s:
            convos = await s.get_conversations()

        lv = self.query_one("#conversation-list", ListView)
        await lv.clear()
        for c in convos:
            await lv.append(
                ConversationItem(
                    peer_id=c["peer_id"],
                    username=c.get("username") or "",
                    unread=c.get("unread_count") or 0,
                )
            )

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
                await chat.load_messages(
                    item.peer_id, item.username, db_path=self.db_path
                )
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
            chat.query_one("#chat-header", Label).update(
                f"{item.username or item.peer_id[:16]} [dim]({fp[:8]}...{fp[-8:]})[/dim] F"
            )
        
        chat.set_status("connecting")
        if self._daemon and self._daemon._dht:
            async def fetch_presence():
                online = await self._daemon._dht.is_online(item.peer_id)
                if chat.current_peer_id == item.peer_id:
                    chat.set_status("ok" if online else "error")
            asyncio.create_task(fetch_presence())
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
        client_msg_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        async with await Storage.open(self.db_path) as s:
            msg_id = await s.save_message(peer_id=peer_id, sender_peer_id=self.peer_id, content=content, is_outgoing=True, client_message_id=client_msg_id)
        
        await chat.append_message(content, "You", now, True, message_id=msg_id, client_message_id=client_msg_id)
        try:
            await self._daemon.send_chat_msg(peer_id, content, client_msg_id)
        except Exception as e:
            self.notify(f"Network faliure: {e}", title="System", severity="error")
            for b in self.query(MessageBubble):
                if b.client_message_id == client_msg_id:
                    b.update_status("failed")

 
    async def handle_incoming(self, peer_id: str, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        chat = self.query_one(ChatPane)
        if chat.current_peer_id == peer_id:
            username = peer_id[:10]
            for item in self.query(ConversationItem):
                if item.peer_id == peer_id:
                    username = item.username or peer_id[:10]
                    break
            self.call_next(chat.append_message, content, username, now, False)
        else:
            self.notify(f"New message from {peer_id[:12]}....")
        self.call_next(self._load_conversations)

    async def handle_status_update(self, msg_id: str, status: str) -> None:
        self.call_next(self._apply_status, msg_id, status)

    def _apply_status(self, msg_id: str, status: str) -> None:
        for b in self.query(MessageBubble):
            if b.client_message_id == msg_id:
                b.update_status(status)
                break

    async def _presence_refresh(self) -> None:
        while True:
            await asyncio.sleep(3)
            try:
                chat = self.query_one(ChatPane)
                if chat.current_peer_id and self._daemon and self._daemon._dht:
                    online = await self._daemon._dht.is_online(chat.current_peer_id)
                    chat.set_status("ok" if online else "error")
            except Exception:
                pass

    async def action_quit(self) -> None:
        if self._daemon and self._daemon.relay_host:
            relay_info = {
                "host": self._daemon.relay_host,
                "port": self._daemon.relay_port,
            }
            self.state_path.write_text(json.dumps(relay_info))
        if self._daemon:
            await self._daemon.stop()
        if self._daemon_task:
            self._daemon_task.cancel()
        if self._presence_task:
            self._presence_task.cancel()
        self.exit()

    def action_new_chat(self) -> None:
        self.push_screen(ChatModal())

    def action_blur_input(self) -> None:
        self.set_focus(self.query_one("#conversation-list"))


def run(
    peer_id: str = "",
    private_key: bytes = b"",
    username: str = "",
    listen_port: int = 63012,
    dht_port: int = 6969,
    bootstrap_nodes: list[tuple[str, int]] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    relay_host: str | None = None,
    relay_port: int = 9100,
) -> None:
    CLISocialApp(
        peer_id=peer_id,
        private_key=private_key,
        username=username,
        listen_port=listen_port,
        dht_port=dht_port,
        bootstrap_nodes=bootstrap_nodes or [],
        db_path=db_path,
        relay_host=relay_host,
        relay_port=relay_port,
    ).run()
