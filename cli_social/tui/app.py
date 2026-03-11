from __future__ import annotations
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Input, ListView, ListItem, Label
from textual.containers import Horizontal, Vertical


class ConversationItem(ListItem):
    def __init__(self, username: str, unread: int = 0) -> None:
        super().__init__()
        self.username = username
        self.unread = unread
        
    def compose(self) -> ComposeResult:
        unread_badge = f" ({self.unread})" if self.unread else ""
        yield Label(f" {self.username}{unread_badge}")
    
class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        width: 25%;
        border-right: solid $primary;
    }
    """
    
    def compose(self) -> ComposeResult:
        yield Label(" Conversations", id="sidebar-title")
        yield ListView(
            ConversationItem("kino", unread=1),
            ConversationItem("bobzi", unread=3),
            ConversationItem("bruno"),
            id="conversation-list"
        )
        
        # kino bobzi bruno , sounds good. These are placeholder names btw , this whole code is just a placeholder for now, will change later

class ChatPane(Vertical):
    DEFAULT_CSS = """
    ChatPane {
        width: 75%;
        padding: 1 2;
    }
    """
    
    def compose(self) -> ComposeResult:
        yield Static(
            "Select a conversation to start chatting",
            id="chat-placeholder"
        )
        
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
        ("escape", "blur-input",    "Focus sidebar")
    ]
    
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            yield Sidebar()
            yield ChatPane()
        yield InputBar()
        yield Footer()
        
    def action_quit(self) -> None:
        self.exit()
        
    def action_new_chat(self) -> None:
        self.notify("New chat , WIP soon!")
        
    def action_blur_input(self) -> None:
        self.set_focus(self.query_one("#conversation-list"))
        
def run():
    CLISocialApp().run()