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

def _require_identity() -> tuple[str, str]:
    if not identity_exists():
        click.echo("No identity found. Run `sxcl init` first.")
        raise SystemExit(1)
    
    passphrase = click.prompt("Passphrase", hide_input=True)
    
    try:
        _, _, peer_id, username = load_identity(passphrase)
        return peer_id, username
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
    peer_id, username = _require_identity()
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