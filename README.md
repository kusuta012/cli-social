# CLI-SXCL



 Decentralized, End-to-End Encrypted Terminal Messenger

**cli-social** (`sxcl`) is a secure, peer to peer messaging application built entirely for the terminal. It uses a Kademlia Distributed Hash Table (DHT) for decentralized peer discovery, the Noise Protocol for absolute end-to-end encryption, and stateless relay nodes to route traffic around NAT's and handle offline messaging.

No central servers, No phone numbers. Just cryptographic identities and your terminal

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [How to Use](#how-to-use)
- [Demo](#demo)
- [Architecture & Security](#architecture--security)
- [Running a node](#running-a-node)
- [Note for Shipwright](#note-for-shipwrights)

---

## Features

- **End-to-End Encryption**: Built on the [Noise Protocol Framework](https://noiseprotocol.org)
- **Cryptographic Identities**: Ed25519 keypairs prevent spoofing and make your `Peer ID` inherently self-certifying.
- **Decentralized Discovery**: A Kademlia-based DHT for finding peers and routing without central registries
- **Secure Local Storage**: SQLite database wrapped in App-level AES-GCM encryption, Your messages are never stored in plaintext on disk
- **Cool TUI**: Cool looking, Terminal User Interface (TUI) built with [Textual](https://textual.texualize.io/)

## Installation

Requires Python 3.12+

```bash
# Install via pip
pip install sxcl
```

Note: depinding on your system, you may want to use `pipx` to install it globally in an isolated environment:`pipx install sxcl`

## How to Use

You can use `sxcl help` to know all commands

### 1. Generate Identity

You must create a identity before using the network. Your keys are encrypted via Argon2 and AES-GCM before being saved to disk.

```bash
sxcl init
```
*Make sure to remember your passphrase, there is no recovery. (This is not a design flaw, but to make it truly secure)*

### 2. Check your ID

Need to share your ID with your friend?

```bash
sxcl whoami
```


### 3. Launch it

Start the TUI 

```bash
sxcl tui
```

- **Ctrl+N**: Start a new chat (requires the Peer ID of your friend)
- **Ctrl+D**: Close the current chat
- **Ctrl+B**: Toggle the siderbar
- **Ctrl+Q**: Quit
- **Ctrl+P**: Open Palette (Themes settings)

### 4. Wipe your Identity?

*There is no recovery, everything is nuked*

```bash
sxcl nuke
```

### 5. You can run daemon in the background

```bash
sxcl daemon
```

*registry command is only for the owner of this project to sign the node registry*

## Demo
[Screencast_20260320_193259.webm](https://github.com/user-attachments/assets/73328586-aff7-4f87-acc7-3b724430ad77)

[Screencast_20260320_193731.webm](https://github.com/user-attachments/assets/379877ce-0fd4-4989-9ff7-f1bf46e5d6bd)

## Architecture & Security

`cli-social` operates entirely peer to peer but uses a relay mesh network to solve NAT traversal and offline delivery.

1.*Identity* Users generate an Ed25519 keypair. The SHA-256 hash of the public key acts as the 64 character Peer ID
2. *Decentralized Hash Table* When the app boots, the local DHT node signs a Kademlia record containing the user's Peer ID, their current Relay server and a timestamp, and announces it to the network
3. *Routing* To send a message, You query the DHT for Person A's Peer ID, The DHT returns Bob's public key and his currently registered Relay Sever.
4. *Encryption* You encrypt your message with `Noise_X_25519_ChaChaPoly_SHA256` and bob's noise public key. Bob then decrypts it with his noise private key.
5. *Delivery* You push the encrypted frame to your home relay, it then passes to Person A's home relay through the relay mesh links, Person A's relay forwards it to them if they are online , or stores it in a local database if he is offline. **The relay cannot read the contents**

## Running a Node

*The network relies on community run relay (but they are signed by only me) and bootstrap nodes, Operators can spin up a node using simple docker setup.*

**[You can find the instructions here](https://github.com/kusuta012/cli-social/blob/main/Node.md)**: 

## Note for Shipwrights

**When you first join the dht network, it might take time to populate your identity and to tell the network that you are here at this relay, so pls wait for atleast 10-15s before the other person adds you, all functions do work, so I want you to check if offline messaging and offline delivery receipts do work. If there is any problem connecting to the dht network, check your firewall, the relay nodes are up 24/7. Verify the fingerprint shown near the username in the chat header, they should match with the other person's fingerprint shown in the chat header**
