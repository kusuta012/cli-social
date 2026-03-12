from __future__ import annotations
# import asyncio
from datetime import datetime, timezone
from typing import Optional
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Input, ListView, ListItem, Label
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from cli_social.storage import Storage
from cli_social.p2p.transport import connect
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
    def __init__(self, content: str, sender: str, sent_at: str, is_outgoing: bool) -> None:
        super().__init__()
        self.msg_content = content
        self.sender = sender
        self.sent_at = sent_at
        self.is_outgoing = is_outgoing
        self.add_class("outgoing" if is_outgoing else "incoming")
    
    def compose(self) -> ComposeResult:
        time_str = self.sent_at[11:16]
        prefix = "you" if self.is_outgoing else self.sender[:10]
        yield Label(f"[dim]{prefix} {time_str}[/dim]")
        yield Label(self.msg_content)
        
        
        
        
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
    
    def compose(self) -> ComposeResult:
        yield Label("Select a conversation", id="chat-header")
        yield ScrollableContainer(id="message-scroll")
    
    async def load_messages(self, peer_id: str, username: str) -> None:
        self.current_peer_id = peer_id
        self.query_one("#chat-header", Label).update(
            f"{username or peer_id[:16]}"
        )
        
        scroll = self.query_one("#message-scroll", ScrollableContainer)
        await scroll.remove_children()
        
        async with await Storage.open() as s:
            messages = await s.get_messages(peer_id, limit=50)
        
        for msg in messages:
            bubble = MessageBubble(
                content = msg["content"],
                sender = username,
                sent_at = msg["sent_at"],
                is_outgoing = bool(msg["is_outgoing"])
            )
            await scroll.mount(bubble)
        scroll.scroll_end(animate=False)
        
    async def append_message(self, content: str, sender: str, sent_at: str, is_outgoing: bool) -> None:
        scroll = self.query_one("#message-scroll", ScrollableContainer)
        bubble = MessageBubble(
            content = content,
            sender = sender,
            sent_at = sent_at,
            is_outgoing = is_outgoing
        )
        await scroll.mount(bubble)
        scroll.scroll_end(animate=True)
        
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
    
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        peer_id = event.value.strip()
        if peer_id:
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
        ("escape", "blur-input",    "Sidebar")
    ]
    def __init__(self, peer_id: str, private_key: bytes, username: str) -> None:
        super().__init__()
        self.peer_id = peer_id
        self.private_key = private_key
        self.username = username
    
    
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            yield Sidebar()
            yield ChatPane()
        yield InputBar()
        yield Footer()
     
    async def on_mount(self) -> None:
        await self._load_conversations()
        self.set_focus(self.query_one("#conversation-list"))
    
    async def _load_conversations(self) -> None:
        async with await Storage.open() as s:
            convos = await s.get_conversations()
            
        lv = self.query_one("#conversation-list", ListView)
        await lv.clear()
        for c in convos:
            await lv.append(ConversationItem(
                peer_id = c["peer_id"],
                username = c.get("username") or "",
                unread = c.get("unread_count") or 0
            ))
            
    async def start_conversation(self, peer_id: str) -> None:
        async with await Storage.open() as s:
            await s.get_or_create_conversation(peer_id)
        await self._load_conversations()
    
    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if not isinstance(item, ConversationItem):
            return
        chat = self.query_one(ChatPane)
        await chat.load_messages(item.peer_id, item.username)
        async with await Storage.open() as s:
            convo_id = await s.get_or_create_conversation(item.peer_id)
            await s.mark_read(convo_id)
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
        try:
            host = "127.0.0.1"
            port = 9000
            session = await connect(host=host, port=port, our_peer_id=self.peer_id, our_private_key=self.private_key, their_peer_id=peer_id)
            await session.send(content)
            session.close()
            
            now = datetime.now(timezone.utc).isoformat()
            async with await Storage.open() as s:
                await s.save_message(
                    peer_id=peer_id,
                    sender_peer_id=self.peer_id,
                    content=content,
                    is_outgoing=True
                )
            chat = self.query_one(ChatPane)
            await chat.append_message(content, "you", now, is_outgoing=True)
        
        except Exception as e:
            self.notify(f"Failed to send: {e}", severity="error")
    
    async def handle_incoming(self, peer_id: str, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        chat = self.query_one(ChatPane)
        if chat.current_peer_id == peer_id:
            await chat.append_message(content, peer_id[:10], now, is_outgoing=False)
        else:
            self.notify(f"New message from {peer_id[:12]}....")
        await self._load_conversations()
        

    def action_quit(self) -> None:
        self.exit()
        
    def action_new_chat(self) -> None:
        self.mount(ChatModal())
        self.query_one("#chat-input", Input).focus()
        
    def action_blur_input(self) -> None:
        self.set_focus(self.query_one("#conversation-list"))
        
def run(peer_id: str = "", private_key: bytes = b"", username: str = "") -> None:
    CLISocialApp(peer_id=peer_id, private_key=private_key, username=username).run()