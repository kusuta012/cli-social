# Relay Node Setup

## Setup

### 1. Create folder (follow exactly)

```bash
mkdir /opt/sxcl-relay
cd /opt/sxcl-relay
```

---

### 2. Create `.env`

Create a file named `.env` and put:

```
RELAY_PORT=YOUR_PORT
RELAY_DB_PATH=YOUR_DB_PATH
```

Example:

```
RELAY_PORT=19853
RELAY_DB_PATH=/data/relay.db
```

---

### 3. Create `docker-compose.yml`

```yaml
version: "3.9"

services:

  relay:
    image: ghcr.io/kusuta012/sxcl:latest
    container_name: sxcl-relay
    restart: unless-stopped

    command: >
      node --relay --relay-port ${RELAY_PORT}

    environment:
      RELAY_PORT: ${RELAY_PORT}
      RELAY_DB_PATH: ${RELAY_DB_PATH}

    ports:
      - "${RELAY_PORT}:${RELAY_PORT}"

    volumes:
      - ./data:/data

  watchtower:
    image: containrrr/watchtower
    container_name: sxcl-watchtower
    restart: unless-stopped
    environment:
      - DOCKER_API_VERSION=1.40
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

    command: --interval 120
```

---

### 4. Start relay

```bash
docker compose up -d
```

---

### 5. Check logs

```bash
docker logs -f sxcl-relay
```

or

```bash
docker compose logs -f
```

---

## Final step

Share your relay address with me on slack (@speedhawks):

```
IP:PORT
```

Example:

```
203.0.113.10:19853
```

I will sign it for the official relay registry.

Thank you for contributing a relay node <3