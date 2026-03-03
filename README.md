# Space Router

Residential IP as a Service for AI Agents.

Space Router provides a single proxy URL that routes agent HTTP traffic through residential IP addresses. No SDK required — configure any HTTP client with the proxy URL and go.

## Architecture

| Component | Language | Description |
|---|---|---|
| **Proxy Gateway** | Python / asyncio | Agent-facing HTTP forward proxy. Authenticates requests, selects a residential node, and tunnels traffic through it. |
| **Coordination API** | Python / FastAPI | Central brain. Node registry, routing decisions, health monitoring, API key management. |
| **Home Node Daemon** | Python / asyncio | Runs on residential machines (macOS). Accepts proxied requests and forwards them from its residential IP. |

```
                     ┌──────────────────┐
   AI Agent ────────►│  Proxy Gateway   │
  (HTTP proxy)       │  :8080 (proxy)   │
                     │  :8081 (mgmt)    │
                     └────────┬─────────┘
                              │
                     ┌────────▼─────────┐
                     │ Coordination API │
                     │  :8000           │
                     │  (Supabase DB)   │
                     └────────┬─────────┘
                              │
                 ┌────────────┼────────────┐
                 ▼            ▼            ▼
           Home Node    Home Node    proxyjet.io
           :9090        :9090        (fallback)
```

## Quick Start (Local Development)

### Prerequisites

- Python 3.12+
- pip

### 1. Coordination API

```bash
cd coordination-api
pip install -r requirements.txt

# Set required environment variables
export SR_SUPABASE_URL=https://your-project.supabase.co
export SR_SUPABASE_SERVICE_KEY=your-service-key
export SR_INTERNAL_API_SECRET=shared-secret

# Proxyjet.io fallback (default proxy provider)
export SR_PROXYJET_HOST=proxy.proxyjet.io
export SR_PROXYJET_PORT=8080
export SR_PROXYJET_USERNAME=your-user
export SR_PROXYJET_PASSWORD=your-pass

# Start the server
python -m app.main
```

### 2. Proxy Gateway

```bash
cd proxy-gateway
pip install -r requirements.txt

# Set required environment variables
export SR_COORDINATION_API_URL=http://localhost:8000
export SR_COORDINATION_API_SECRET=shared-secret

# Start the server (proxy on :8080, management API on :8081)
python -m app.main
```

### 3. Home Node Daemon

```bash
cd home-node
pip install -r requirements.txt

# Set required environment variables
export SR_COORDINATION_API_URL=http://localhost:8000

# For local dev, set PUBLIC_IP so it doesn't try external IP detection
export SR_PUBLIC_IP=127.0.0.1

# Optional: set node metadata
export SR_NODE_LABEL=my-macbook
export SR_NODE_REGION=us-west

# Start the daemon (listens on :9090)
python -m app.main
```

### 4. Database Setup

Run `coordination-api/schema.sql` in the Supabase SQL Editor to create the required tables (`api_keys`, `nodes`, `route_outcomes`, `request_logs`).

### 5. Create an API Key

```bash
curl -X POST http://localhost:8000/api-keys \
  -H "Content-Type: application/json" \
  -d '{"name": "My Agent"}'
```

Save the `api_key` from the response — it's only shown once.

### 6. Test End-to-End

```bash
# Send a request through the full pipeline:
# Agent → Proxy Gateway → Home Node → target
curl -x http://sr_live_YOUR_API_KEY@localhost:8080 http://httpbin.org/ip
```

### Usage in Code

Agents configure their HTTP client with the Space Router proxy URL:

```python
import httpx

proxy_url = "http://sr_live_YOUR_API_KEY@localhost:8080"

async with httpx.AsyncClient(proxy=proxy_url) as client:
    response = await client.get("https://target-website.com/data")
    print(response.status_code)
```

Or with curl:

```bash
curl -x http://sr_live_YOUR_API_KEY@localhost:8080 https://example.com
```

## Production Deployment

### Coordination API (Fly.io)

```bash
cd coordination-api

# Set secrets
fly secrets set \
  SR_SUPABASE_URL=https://your-project.supabase.co \
  SR_SUPABASE_SERVICE_KEY=your-service-key \
  SR_INTERNAL_API_SECRET=your-strong-secret \
  SR_PROXYJET_HOST=proxy.proxyjet.io \
  SR_PROXYJET_PORT=8080 \
  SR_PROXYJET_USERNAME=your-user \
  SR_PROXYJET_PASSWORD=your-pass

fly deploy
```

### Proxy Gateway (Fly.io)

```bash
cd proxy-gateway

fly secrets set \
  SR_COORDINATION_API_URL=https://coordination.spacerouter.io \
  SR_COORDINATION_API_SECRET=your-strong-secret

fly deploy
```

### Home Node Daemon (macOS)

The Home Node runs on residential machines. For production on macOS:

**Option A: Run directly**

```bash
cd home-node
pip install -r requirements.txt

export SR_COORDINATION_API_URL=https://coordination.spacerouter.io
export SR_NODE_LABEL=macbook-home
export SR_NODE_REGION=us-west

python -m app.main
```

**Option B: Install as a macOS launchd service (auto-start on boot)**

```bash
# 1. Install the code
sudo mkdir -p /opt/spacerouter/home-node
sudo cp -r home-node/* /opt/spacerouter/home-node/
cd /opt/spacerouter/home-node && pip install -r requirements.txt

# 2. Edit the plist to set your Coordination API URL and node metadata
vim home-node/launchd/com.spacerouter.homenode.plist

# 3. Install and start the service
cp home-node/launchd/com.spacerouter.homenode.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.spacerouter.homenode.plist

# Check status
launchctl list | grep spacerouter

# View logs
tail -f /tmp/spacerouter-homenode.stdout.log
tail -f /tmp/spacerouter-homenode.stderr.log

# Stop the service
launchctl unload ~/Library/LaunchAgents/com.spacerouter.homenode.plist
```

**Option C: Docker**

```bash
cd home-node
docker build -t spacerouter-homenode .
docker run -d \
  -p 9090:9090 \
  -e SR_COORDINATION_API_URL=https://coordination.spacerouter.io \
  -e SR_NODE_LABEL=docker-node \
  -e SR_NODE_REGION=us-west \
  spacerouter-homenode
```

## Running Tests

```bash
# Proxy Gateway (31 tests)
cd proxy-gateway && pytest tests/ -v

# Coordination API (22 tests)
cd coordination-api && pytest tests/ -v

# Home Node Daemon
cd home-node && pytest tests/ -v
```

## Configuration

All settings are via environment variables with the `SR_` prefix.

### Proxy Gateway

| Variable | Default | Description |
|---|---|---|
| `SR_PROXY_PORT` | 8080 | Port for the proxy server |
| `SR_MANAGEMENT_PORT` | 8081 | Port for health/metrics API |
| `SR_COORDINATION_API_URL` | — | Coordination API base URL |
| `SR_COORDINATION_API_SECRET` | — | Shared secret for internal API auth |
| `SR_SUPABASE_URL` | — | Supabase project URL for request logging |
| `SR_SUPABASE_SERVICE_KEY` | — | Supabase service role key |
| `SR_DEFAULT_RATE_LIMIT_RPM` | 60 | Default requests per minute per API key |
| `SR_NODE_REQUEST_TIMEOUT` | 30.0 | Timeout (seconds) for node requests |
| `SR_AUTH_CACHE_TTL` | 300 | Seconds to cache auth validation results |

### Coordination API

| Variable | Default | Description |
|---|---|---|
| `SR_PORT` | 8000 | API server port |
| `SR_INTERNAL_API_SECRET` | — | Shared secret for internal endpoints |
| `SR_SUPABASE_URL` | — | Supabase project URL |
| `SR_SUPABASE_SERVICE_KEY` | — | Supabase service role key |
| `SR_PROXYJET_HOST` | — | Proxyjet.io proxy hostname |
| `SR_PROXYJET_PORT` | 8080 | Proxyjet.io proxy port |
| `SR_PROXYJET_USERNAME` | — | Proxyjet.io auth username |
| `SR_PROXYJET_PASSWORD` | — | Proxyjet.io auth password |

### Home Node Daemon

| Variable | Default | Description |
|---|---|---|
| `SR_NODE_PORT` | 9090 | TCP server port |
| `SR_COORDINATION_API_URL` | http://localhost:8000 | Coordination API base URL |
| `SR_PUBLIC_IP` | (auto-detected) | Public IP address; auto-detected if empty |
| `SR_NODE_TYPE` | residential | Node type for registration |
| `SR_NODE_LABEL` | — | Human-readable label for this node |
| `SR_NODE_REGION` | — | Region identifier (e.g., us-west, eu-central) |
| `SR_BUFFER_SIZE` | 65536 | TCP read buffer size |
| `SR_REQUEST_TIMEOUT` | 30.0 | Timeout (seconds) for connecting to target servers |
| `SR_RELAY_TIMEOUT` | 300.0 | Max duration (seconds) for a CONNECT tunnel relay |

## API Reference

### Proxy Gateway (port 8080)

Standard HTTP forward proxy. Agents send requests through it like any other proxy.

**Authentication:** `Proxy-Authorization: Basic base64(api_key:)`

**Response headers:**

| Header | Description |
|---|---|
| `X-SpaceRouter-Node` | ID of the node that served the request |
| `X-SpaceRouter-Request-Id` | Unique request ID for debugging |

**Error codes:** 407 (auth required), 429 (rate limited), 502 (upstream error), 503 (no nodes)

### Coordination API (port 8000)

#### API Key Management

| Endpoint | Description |
|---|---|
| `POST /api-keys` | Create a new API key |
| `GET /api-keys` | List all API keys |
| `DELETE /api-keys/{id}` | Revoke an API key |

#### Node Management

| Endpoint | Description |
|---|---|
| `POST /nodes` | Register a new proxy node |
| `GET /nodes` | List all nodes |
| `PATCH /nodes/{id}/status` | Update node status (online/offline/draining) |
| `DELETE /nodes/{id}` | Remove a node |

#### Internal Endpoints (used by Proxy Gateway)

| Endpoint | Description |
|---|---|
| `POST /internal/auth/validate` | Validate an API key hash |
| `GET /internal/route/select` | Select best available node |
| `POST /internal/route/report` | Report routing outcome |

#### Health

| Endpoint | Description |
|---|---|
| `GET /healthz` | Liveness check |
| `GET /readyz` | Readiness check |

### Proxy Gateway Management (port 8081)

| Endpoint | Description |
|---|---|
| `GET /healthz` | Liveness check |
| `GET /readyz` | Readiness check |
| `GET /metrics` | Request counts and connection stats |

### Home Node Daemon (port 9090)

The Home Node accepts raw TCP connections from the Proxy Gateway. It supports:

- **CONNECT tunneling** — for HTTPS traffic, establishes a bidirectional TCP tunnel to the target
- **HTTP forward proxying** — for plain HTTP traffic, forwards the request and streams the response back

The Home Node strips `X-SpaceRouter-*` and `Proxy-Authorization` headers before forwarding to target servers.

On startup it auto-registers with the Coordination API (`POST /nodes`) and sets status to `offline` on graceful shutdown.
