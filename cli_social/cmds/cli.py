from __future__ import annotations
import asyncio
from pathlib import Path
import click
from cli_social.identity import (
    generate_identity,
    load_identity,
    identity_exists,
    _key_file_for
)
import json
import time
import platformdirs
import logging
from cli_social.storage import db_path_for
from cli_social.p2p.daemon import Daemon
from cli_social.p2p.dht import DHTNode
from cli_social.p2p.relay import RelayServer
from cli_social.identity import sign_registry_doc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DEFAULT_BOOTSTRAP_NODES = [
    ("64.225.108.241", 52342),
    ("139.59.77.182", 51560),
    ("45.55.209.227", 52585),
    ("138.197.209.223", 51311)
]


DATA_DIR_OPTION = click.option(
    "--data-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="db path",
    envvar="SXCL_DATA_DIR"
)

def _require_identity(data_dir: Path | None) -> tuple[str, bytes, str, Ed25519PublicKey, Ed25519PrivateKey]:
    key_file = _key_file_for(data_dir)
    if not identity_exists(key_file):
        click.echo("\n Welcome to CLI-SXCL! No identity found")
        do_setup = click.prompt("Would you like to set one up now? [Y/n]", default="y")
        if do_setup.lower().startswith('y'):
            init.callback(data_dir)
            click.echo("\n---Setup Complete! ---\n")
        else:
            click.echo("Okay! Run `sxcl init` when you are ready")
            raise SystemExit(1)
    
    passphrase = click.prompt("Enter your passphrase", hide_input=True)
    
    try:
        private_key, public_key, peer_id, username, noise_private_bytes = load_identity(passphrase, key_file)
        return peer_id, noise_private_bytes, username, public_key, private_key
    except ValueError:
        click.echo("Wrong passphrase! you forgot your password??")
        raise SystemExit(1)
    
@click.group()
def main():
    pass

@main.group()
def registry():
    pass

@registry.command()
@click.argument("input_file", type=click.File('r'))
@click.argument("output_file", type=click.File('w'))
@DATA_DIR_OPTION
def sign(input_file, output_file, data_dir):
    key_file = _key_file_for(data_dir)
    if not identity_exists(key_file):
        click.echo("No identity found. Run `sxcl init` first.")
        raise SystemExit(1)
    
    passphrase = click.prompt("Enter passphrase for signing key", hide_input=True)
    try:
        private_key, _, _, _, _ = load_identity(passphrase, key_file)
    except ValueError:
        click.echo("Wrong passphrase! you forgot your password?? dumbahh")
        raise SystemExit(1)
    
    registry_data = json.load(input_file)
    registry_data["version"] = registry_data.get("version", 0) + 1
    registry_data["timestamp"] = int(time.time())
    registry_data["signatures"] = {}
    signed_registry = sign_registry_doc(private_key, registry_data)
    json.dump(signed_registry, output_file, indent=2)
    click.echo(f"signed registry wrote it to {output_file.name}")

@main.command()
@DATA_DIR_OPTION
def init(data_dir):
    key_file = _key_file_for(data_dir)
    key_path = str(key_file).replace(str(Path.home()), "~")
    if identity_exists(key_file):
        click.echo(f"Identity already exists at {key_file}")
        click.echo("Delete it mannually if you want to start new (you will lose your account tho, be careful)")
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
    peer_id = generate_identity(passphrase, username, key_file)
    
    click.echo("\n Identity created")
    click.echo(f"   Username : {username or '(none)'}")
    click.echo(f"   Peer ID  : {peer_id}")
    click.echo(f"   Key File : {key_path}")
    click.echo("\n Remember your passphrase, there is no recovery")

@main.command()
@DATA_DIR_OPTION
def nuke(data_dir):
    key_file = _key_file_for(data_dir)
    db_path = db_path_for(data_dir)

    click.secho("\n WARNING: Thid will permanently delete your identity and ALL messages!", fg="red", bold=True)
    confirm = click.prompt("Are you ABSOLUTELY sure? Type 'DELETE' to confirm", default="")

    if confirm == "DELETE":
        if key_file.exists():
            key_file.unlink()
            click.echo(f"[-] Deleted identity at {key_file}")
        if db_path.exists():
            db_path.unlink()
            click.echo(f"[-] Deleted database at {db_path}")
        
        state_path = db_path.parent / "state.json"
        if state_path.exists():
            state_path.unlink()
        
        click.secho("\nWipe complete. You are a ghost again!", fg="green", bold=True)
    else:
        click.echo("\nWipe cancelled. Your identity and data remain intact")

    
@main.command()
@DATA_DIR_OPTION
def whoami(data_dir):
    peer_id, _, username, public_key, _ = _require_identity(data_dir)
    pub_key_hex = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    click.echo(f"   Username : {username or '(none)'}")
    click.echo(f"   Peer ID  : {peer_id}")
    click.echo(f"   Pub Key  : {pub_key_hex}")

@main.command()
@DATA_DIR_OPTION
@click.option("--dht-port", default=6969, help="DHT listen port")
@click.option("--bootstrap", multiple=True, help="bootstrap nodes as host:port")
@click.option("--relay", default=None, help="relay node as host:port")
def tui(dht_port, bootstrap, relay, data_dir):

    log_dir = Path(platformdirs.user_log_dir("cli-social"))
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
    filename=str(Path(data_dir or log_dir) / "debug.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    
    from cli_social.tui import run
    peer_id, private_key, username, _, ed25519_key = _require_identity(data_dir)    
    
    bootstrap_nodes = []
    if bootstrap:
        for node in bootstrap:
            h, p = node.rsplit(":", 1)
            bootstrap_nodes.append((h, int(p)))
    else:
        bootstrap_nodes = DEFAULT_BOOTSTRAP_NODES
        
    relay_host, relay_port = None, 9100
    if relay:
        rh, rp = relay.rsplit(":", 1)
        relay_host, relay_port = rh, int(rp)
        
    run(peer_id=peer_id, private_key=private_key, username=username, dht_port=dht_port, bootstrap_nodes=bootstrap_nodes, db_path=db_path_for(data_dir), relay_host=relay_host, relay_port=relay_port, ed25519_key=ed25519_key)

@main.command()
@DATA_DIR_OPTION
@click.option("--dht-port", default=6969, help="DHT listen port")
@click.option("--bootstrap", multiple=True, help="Bootstrap nodes as host:port")
def daemon(dht_port, bootstrap, data_dir):
    peer_id, private_key, username, _, ed25519_key = _require_identity(data_dir)
    
    bootstrap_nodes = []
    if bootstrap:
        for node in bootstrap:
            h, p = node.rsplit(":", 1)
            bootstrap_nodes.append((h, int(p)))
    else:
        bootstrap_nodes = DEFAULT_BOOTSTRAP_NODES
        
    async def _run():
        d = Daemon(
            peer_id=peer_id,
            private_key=private_key,
            username=username,
            dht_port=dht_port,
            bootstrap_nodes=bootstrap_nodes,
            db_path=db_path_for(data_dir),
            ed25519_private_key=ed25519_key
        )
        await d.start()
        click.echo(f"DHT port {dht_port}")
        click.echo(f"Peer ID {peer_id[:16]}....{peer_id[-8:]}")
        click.echo("Press Ctrl+C to stop")
        try:
            await d.run_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await d.stop()
            click.echo("\n Daemon stopped")
            
    asyncio.run(_run())
  
@main.command()
@click.option("--host", default="0.0.0.0", help="the host to listen on")
@click.option("--relay-port", default=19853, help="tcp port for relay server")
@click.option("--dht-port", default=6969, help="UDP port for the dht node")
@click.option("--relay", is_flag=True, help="run as a message relay node")
@click.option("--bootstrap", is_flag=True, help="run as dht bootstrap node")
def node(host, relay_port, dht_port, relay, bootstrap):
    if not (relay or bootstrap):
        click.echo("You must specify role (--relay, --boostrap)")
        return
    
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    
    async def _run_node():
        tasks = []
        if bootstrap:
            dht = DHTNode(peer_id="bootstrap_node", host=host, port=dht_port, bootstrap_nodes=DEFAULT_BOOTSTRAP_NODES)
            tasks.append(dht.start())
            click.echo(f"DHT bootstrap node listening on UDP {host}:{dht_port}")
            
        if relay:
            relay_server = RelayServer(host=host, port=relay_port)
            tasks.append(asyncio.create_task(relay_server.start()))
            click.echo(f"Relay node listening on TCP {host}:{relay_port}")
        
        # if this crashes
        if tasks:
            await asyncio.gather(*tasks)
            await asyncio.Future()
        else:
            click.echo("No tasks to run")
    
    try:
        asyncio.run(_run_node())
    except KeyboardInterrupt:
        click.echo("\nShutting down node!") 
                   