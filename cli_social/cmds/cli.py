from __future__ import annotations
import asyncio
from pathlib import Path
import click
from cli_social.identity import (
    generate_identity,
    load_identity,
    identity_exists,
    DEFAULT_KEY_FILE
)
from cli_social.storage import Storage
from cli_social.p2p.daemon import Daemon
from cli_social.p2p.transport import connect
from cli_social.p2p.dht import DHTNode
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

def _require_identity() -> tuple[str, bytes, str]:
    if not identity_exists():
        click.echo("No identity found. Run `sxcl init` first.")
        raise SystemExit(1)
    
    passphrase = click.prompt("Passphrase", hide_input=True)
    
    try:
        private_key, _, peer_id, username = load_identity(passphrase)
        private_key_bytes = private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        return peer_id, private_key_bytes, username
    except ValueError:
        click.echo("Wrong passphrase! you forgot your password?? dumbahh")
        raise SystemExit(1)
    
@click.group()
def main():
    pass

@main.command()
def init():
    key_path = str(DEFAULT_KEY_FILE).replace(str(Path.home()), "~")
    if identity_exists():
        click.echo(f"Identity already exists at {DEFAULT_KEY_FILE}")
        click.echo(f"Delete it mannually if you want to start new (you will lose your account tho, be careful)")
        return

    click.echo("Creating a new identity..\n")
    username = click.prompt("Username (this is optional, press enter to skip)", default="")
    
    while True:
        passphrase = click.prompt("Passphrase", hide_input=True)
        if len(passphrase) < 8:
            click.echo("Passphrase must be at least 8 characters, (more efforts)")
            continue
        confirm = click.prompt("Confirm Passphrase", hide_input=True)
        if passphrase != confirm:
            click.echo("Passphrase don't match, try again")
            continue
        break
    
    click.echo("\n Deriving key for you (this takes a few seconds) be patient.")
    peer_id = generate_identity(passphrase, username)
    
    click.echo(f"\n Identity created")
    click.echo(f"   Username : {username or '(none)'}")
    click.echo(f"   Peer ID  : {peer_id}")
    click.echo(f"   Key File : {key_path}")
    click.echo(f"\n Remember your passphrase, there is no recovery")
    
@main.command()
def whoami():
    peer_id, _, username = _require_identity()
    click.echo(f"   Username : {username or '(none)'}")
    click.echo(f"   Peer ID  : {peer_id}")

@main.command()
def contacts():
    async def _list() -> list[dict[str, str]]:
        async with await Storage.open() as s:
            return await s.get_contacts()
        
    results: list[dict[str, str]] = asyncio.run(_list())
    
    if not results:
        click.echo("No contacts yet, add them to start messaging")
        return
    
    click.echo(f"\n{'Username':<20} {'Peer ID'}")
    click.echo("-" * 70)
    for c in results:
        name = c["username"] or "(no name)"
        click.echo(f"{name:<20} {c['peer_id']}")

@main.command()
def tui():
    from cli_social.tui import run
    run()

@main.command()
@click.option("--port", default=9000, help="TCP listen port")
@click.option("--dht-port", default=6969, help="DHT listen port")
@click.option("--bootstrap", multiple=True, help="Bootstrap nodes as host:port")
def daemon(port, dht_port, bootstrap):
    peer_id, private_key, username = _require_identity()
    
    bootstrap_nodes = []
    for node in bootstrap:
        host, p = node.rsplit(":", 1)
        bootstrap_nodes.append((host, int(p)))
        
    async def _run():
        d = Daemon(
            peer_id=peer_id,
            private_key=private_key,
            username=username,
            listen_port=port,
            dht_port=dht_port,
            bootstrap_nodes=bootstrap_nodes
        )
        await d.start()
        click.echo(f"Daemon running on port {port}")
        click.echo(f"DHT port {dht_port}")
        click.echo(f"Peer ID {peer_id[:16]}....{peer_id[-8:]}")
        click.echo(f"Press Ctrl+C to stop")
        try:
            await d.run_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await d.stop()
            click.echo("\n Daemon stopped")
            
    asyncio.run(_run())

@main.command()
@click.argument("peer_id")
@click.option("--username", default="", help="Optional username for this contact")
@click.option("--public-key", default="", help="contact's public key (hex)")
def add(peer_id, username, public_key):
    async def _add():
        async with await Storage.open() as s:
            await s.add_contact(
                peer_id=peer_id,
                username=username,
                public_key=public_key
            )
    asyncio.run(_add())
    click.echo(f"Contact added {username or peer_id}")
    
@main.command()
@click.argument("peer_id")
@click.argument("message")
@click.option("--host", default=None, help="Direct host (to skip the dht lookup)")
@click.option("--port", default=9000, help="Remote peer root")
def send(peer_id, message, host, port):
    our_peer_id, private_key, _ = _require_identity()
    
    async def _send():
        target_host = host
        if not target_host:
            click.echo("looking up peer in DHT..")
            dht = DHTNode(peer_id=our_peer_id, port=0)
            await dht.start()
            peer_info = await dht.lookup(peer_id)
            await dht.stop()
            if not peer_info:
                click.echo(f"Peer {peer_id[:16]} not found in DHT", err=True)
                return
            target_host = peer_info.host
            nonlocal port
            port = peer_info.port
            click.echo(f"found peer at {peer_id[:16]}.,")
        
        click.echo(f"Connecting to {peer_id[:16]}..")
        session = await connect(
            host=target_host,
            port=port,
            our_peer_id=our_peer_id,
            our_private_key=private_key,
            their_peer_id=peer_id
        )
        await session.send(message)
        
        async with await Storage.open() as s:
            await s.save_message(
                peer_id=peer_id,
                sender_peer_id=our_peer_id,
                content=message,
                is_outgoing=True
            )
        
        session.close()
        click.echo(f"Message sent")
    asyncio.run(_send())