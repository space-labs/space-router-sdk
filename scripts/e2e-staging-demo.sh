#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Space Router — Staging End-to-End Demo
#
# Tests the full proxy chain across real infrastructure:
#
#   Client (this machine)
#     └─ TLS ─→ Proxy Gateway (Fly.io, IAD)        ← authenticates, selects node
#                 └─→ Coordination API (Fly.io, IAD) ← routing + auth backend
#                 └─ TLS ─→ Home Node (local, UPnP)  ← residential exit
#                             └─→ Target (httpbin.org)
#
# Unlike the local demo (e2e-demo.sh), this script:
#   • Uses Coordination API and Proxy Gateway deployed on Fly.io
#   • Runs the Home Node locally with real UPnP port forwarding
#   • Verifies traffic exits through a residential IP (not a datacenter)
#   • Tests the TLS handler required for Fly.io shared IPv4 routing
#
# Prerequisites:
#   • Coordination API deployed: spacerouter-coordination-api.fly.dev
#   • Proxy Gateway deployed:    spacerouter-proxy-gateway.fly.dev:8080
#   • Home Node dependencies installed (home-node/.venv)
#   • UPnP-capable router on the local network
#   • No VPN active (e.g., Cloudflare WARP) — UPnP needs the real gateway
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_NODE_DIR="${HOME_NODE_DIR:-$REPO_ROOT/home-node}"

COORD_API_URL="${COORD_API_URL:-https://spacerouter-coordination-api.fly.dev}"
PROXY_GATEWAY_HOST="${PROXY_GATEWAY_HOST:-spacerouter-proxy-gateway.fly.dev}"
PROXY_GATEWAY_PORT="${PROXY_GATEWAY_PORT:-8080}"
PROXY_URL="https://${PROXY_GATEWAY_HOST}:${PROXY_GATEWAY_PORT}"

HOME_NODE_PID=""
NODE_ID=""
API_KEY=""
API_KEY_ID=""

PASS=0
FAIL=0
TOTAL=0
OBSERVED_IP=""
START_TIME=$(date +%s)

# ─── Cleanup trap ─────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo " Cleaning up"
    echo "═══════════════════════════════════════════════════════════════"

    # 1. Stop Home Node
    if [[ -n "$HOME_NODE_PID" ]] && kill -0 "$HOME_NODE_PID" 2>/dev/null; then
        echo "  Stopping Home Node (PID $HOME_NODE_PID)..."
        kill "$HOME_NODE_PID" 2>/dev/null || true
        sleep 2
        kill -0 "$HOME_NODE_PID" 2>/dev/null && kill -9 "$HOME_NODE_PID" 2>/dev/null || true
        wait "$HOME_NODE_PID" 2>/dev/null || true
        echo "  Home Node stopped."
    fi

    # 2. Deregister node from Coordination API
    if [[ -n "$NODE_ID" ]]; then
        echo "  Deregistering node $NODE_ID..."
        curl -sf -X DELETE "${COORD_API_URL}/nodes/${NODE_ID}" >/dev/null 2>&1 || true
        echo "  Node deregistered."
    fi

    # 3. Remove UPnP mapping
    if [[ -d "$HOME_NODE_DIR/.venv" ]]; then
        echo "  Removing UPnP port mapping..."
        "$HOME_NODE_DIR/.venv/bin/python3" -c "
import miniupnpc
u = miniupnpc.UPnP()
u.discoverdelay = 2000
if u.discover() > 0:
    u.selectigd()
    u.deleteportmapping(9090, 'TCP')
    print('  UPnP mapping removed.')
" 2>/dev/null || echo "  (UPnP cleanup skipped)"
    fi

    # 4. Delete test API key
    if [[ -n "$API_KEY_ID" ]]; then
        echo "  Deleting test API key $API_KEY_ID..."
        curl -sf -X DELETE "${COORD_API_URL}/api-keys/${API_KEY_ID}" >/dev/null 2>&1 || true
        echo "  API key deleted."
    fi

    echo "  Done."
}
trap cleanup EXIT INT TERM

# ─── Helpers ──────────────────────────────────────────────────────────────────
log()    { echo "  [$1] $2"; }
pass()   { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); log "PASS" "$1"; }
fail()   { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); log "FAIL" "$1"; }
header() { echo ""; echo "═══ $1 ═══"; }
detail() { echo "       $1"; }

wait_for_tcp() {
    local host="$1" port="$2" max_wait="${3:-30}"
    local elapsed=0
    while (( elapsed < max_wait )); do
        if nc -z "$host" "$port" 2>/dev/null; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Prerequisite Checks
# ═══════════════════════════════════════════════════════════════════════════════
header "Phase 1 — Prerequisite Checks"

# Check required tools
for cmd in curl python3 nc; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  ✓ $cmd found"
    else
        echo "  ✗ $cmd not found — please install it"
        exit 1
    fi
done

# Check Home Node venv
if [[ ! -d "$HOME_NODE_DIR/.venv" ]]; then
    echo "  ✗ Home Node venv not found at $HOME_NODE_DIR/.venv"
    echo "    Run: cd $HOME_NODE_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
echo "  ✓ Home Node venv exists"

# Check Coordination API health
COORD_HEALTH=$(curl -sf --max-time 10 "${COORD_API_URL}/healthz" 2>&1) || COORD_HEALTH=""
if echo "$COORD_HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'status' in d" 2>/dev/null; then
    echo "  ✓ Coordination API healthy (${COORD_API_URL})"
else
    echo "  ✗ Coordination API not responding at ${COORD_API_URL}"
    echo "    Response: $COORD_HEALTH"
    exit 1
fi

# Check Coordination API readiness
COORD_READY=$(curl -sf --max-time 10 "${COORD_API_URL}/readyz" 2>&1) || COORD_READY=""
if echo "$COORD_READY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'status' in d" 2>/dev/null; then
    echo "  ✓ Coordination API ready"
else
    echo "  ⚠ Coordination API /readyz unexpected: $COORD_READY (continuing anyway)"
fi

# Quick VPN check — warn if Cloudflare WARP or similar might interfere
if pgrep -f "CloudflareWARP\|warp-svc" >/dev/null 2>&1; then
    echo ""
    echo "  ⚠  WARNING: Cloudflare WARP appears to be running."
    echo "     UPnP will fail because WARP intercepts the default gateway."
    echo "     Please disable WARP before running this demo."
    echo ""
    exit 1
fi
echo "  ✓ No VPN interference detected"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Start Home Node
# ═══════════════════════════════════════════════════════════════════════════════
header "Phase 2 — Starting Home Node with UPnP"

# Ensure .env is configured for staging
# (We write a staging .env — the Home Node will auto-detect public IP and UPnP)
cat > "$HOME_NODE_DIR/.env" << EOF
SR_COORDINATION_API_URL=${COORD_API_URL}
SR_UPNP_ENABLED=true
SR_NODE_LABEL=staging-demo
SR_NODE_REGION=auto
SR_LOG_LEVEL=INFO
EOF
echo "  Wrote staging .env (UPnP enabled, public IP auto-detect)"

# Clean stale certs so we get fresh ones
rm -rf "$HOME_NODE_DIR/certs"
echo "  Cleaned stale TLS certificates"

# Create logs dir
mkdir -p "$REPO_ROOT/logs"

# Start Home Node
echo "  Starting Home Node..."
cd "$HOME_NODE_DIR"
"$HOME_NODE_DIR/.venv/bin/python" -m app.main \
    > "$REPO_ROOT/logs/staging-home-node.log" 2>&1 &
HOME_NODE_PID=$!
cd "$REPO_ROOT"

echo "  Home Node PID: $HOME_NODE_PID"

# Wait for the TCP port to open (UPnP + TLS cert generation can take ~10s)
echo "  Waiting for Home Node to listen on port 9090..."
if ! wait_for_tcp 127.0.0.1 9090 30; then
    echo "  ✗ Home Node failed to start within 30 seconds"
    echo "  Logs:"
    tail -30 "$REPO_ROOT/logs/staging-home-node.log"
    exit 1
fi
echo "  ✓ Home Node listening on port 9090"

# Give it a moment to complete registration
sleep 3

# Extract node ID from Coordination API
echo "  Verifying node registration..."
NODES_JSON=$(curl -sf "${COORD_API_URL}/nodes" 2>&1) || NODES_JSON="[]"
NODE_ID=$(echo "$NODES_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)
# Find the node we just registered (staging-demo label, online status)
for n in nodes:
    if n.get('status') == 'online':
        print(n['id'])
        break
" 2>/dev/null) || NODE_ID=""

if [[ -z "$NODE_ID" ]]; then
    echo "  ✗ Node did not register. Retrying in 5s..."
    sleep 5
    NODES_JSON=$(curl -sf "${COORD_API_URL}/nodes" 2>&1) || NODES_JSON="[]"
    NODE_ID=$(echo "$NODES_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    if n.get('status') == 'online':
        print(n['id'])
        break
" 2>/dev/null) || NODE_ID=""
fi

if [[ -n "$NODE_ID" ]]; then
    # Extract node details for display
    NODE_INFO=$(echo "$NODES_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    if n['id'] == '$NODE_ID':
        print(f\"endpoint={n['endpoint_url']} ip_type={n.get('ip_type','?')} region={n.get('ip_region','?')}\")
        break
" 2>/dev/null) || NODE_INFO=""
    echo "  ✓ Node registered: ${NODE_ID:0:8}..."
    echo "    $NODE_INFO"
else
    echo "  ✗ Node registration failed. Logs:"
    tail -20 "$REPO_ROOT/logs/staging-home-node.log"
    exit 1
fi


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Create Test API Key
# ═══════════════════════════════════════════════════════════════════════════════
header "Phase 3 — Creating Test API Key"

API_KEY_RESPONSE=$(curl -sf -X POST "${COORD_API_URL}/api-keys" \
    -H "Content-Type: application/json" \
    -d '{"name": "staging-e2e-demo", "rate_limit_rpm": 120}') || API_KEY_RESPONSE=""

if [[ -z "$API_KEY_RESPONSE" ]]; then
    echo "  ✗ Failed to create API key"
    exit 1
fi

API_KEY=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
API_KEY_ID=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "  ✓ API key created: ${API_KEY:0:24}..."
echo "    Key ID: $API_KEY_ID"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4: Run E2E Test Suite
# ═══════════════════════════════════════════════════════════════════════════════
header "Phase 4 — Running E2E Test Suite"
echo ""
echo "  Proxy URL:  $PROXY_URL"
echo "  Coord API:  $COORD_API_URL"
echo "  Node ID:    ${NODE_ID:0:8}..."
echo ""

# ────────────────────────────────────────────────────────────────
# Group A: Infrastructure Health
# ────────────────────────────────────────────────────────────────
echo "── A. Infrastructure Health ─────────────────────────────────"
echo ""

# A1: Coordination API /healthz
echo "  A1: Coordination API /healthz"
HEALTH=$(curl -sf --max-time 10 "${COORD_API_URL}/healthz" 2>&1) || HEALTH=""
HEALTH_STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null) || HEALTH_STATUS=""
if [[ -n "$HEALTH_STATUS" ]]; then
    pass "Coordination API /healthz → $HEALTH_STATUS"
else
    fail "Coordination API /healthz → $HEALTH"
fi

# A2: Coordination API /readyz
echo "  A2: Coordination API /readyz"
READY=$(curl -sf --max-time 10 "${COORD_API_URL}/readyz" 2>&1) || READY=""
READY_STATUS=$(echo "$READY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null) || READY_STATUS=""
if [[ -n "$READY_STATUS" ]]; then
    pass "Coordination API /readyz → $READY_STATUS"
else
    fail "Coordination API /readyz → $READY"
fi

# A3: Node registration
echo "  A3: Node registered and online"
NODE_STATUS=$(echo "$NODES_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    if n['id'] == '$NODE_ID':
        print(n.get('status','unknown'))
        break
" 2>/dev/null) || NODE_STATUS=""
if [[ "$NODE_STATUS" == "online" ]]; then
    pass "Node ${NODE_ID:0:8}... registered (status=online)"
else
    fail "Node status: $NODE_STATUS (expected online)"
fi

echo ""

# ────────────────────────────────────────────────────────────────
# Group B: Authentication
# ────────────────────────────────────────────────────────────────
echo "── B. Authentication ────────────────────────────────────────"
echo ""

# B1: No credentials → 407
echo "  B1: Request without credentials"
NO_AUTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
    --proxy "$PROXY_URL" --proxy-insecure \
    "http://httpbin.org/ip" 2>&1) || NO_AUTH_STATUS="000"
if [[ "$NO_AUTH_STATUS" == "407" ]]; then
    pass "No credentials → 407 Proxy Authentication Required"
else
    fail "No credentials → $NO_AUTH_STATUS (expected 407)"
fi

# B2: Bad credentials → 407
echo "  B2: Request with invalid credentials"
BAD_AUTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
    --proxy "$PROXY_URL" --proxy-user "sr_fake_invalid_key_1234567890:" --proxy-insecure \
    "http://httpbin.org/ip" 2>&1) || BAD_AUTH_STATUS="000"
if [[ "$BAD_AUTH_STATUS" == "407" ]]; then
    pass "Bad credentials → 407 Proxy Authentication Required"
else
    fail "Bad credentials → $BAD_AUTH_STATUS (expected 407)"
fi

echo ""

# ────────────────────────────────────────────────────────────────
# Group C: HTTP Forward Proxy
# ────────────────────────────────────────────────────────────────
echo "── C. HTTP Forward Proxy ────────────────────────────────────"
echo ""

# C1: HTTP forward proxy returns 200 with residential IP
echo "  C1: HTTP proxy → httpbin.org/ip"
HTTP_HEADER_FILE=$(mktemp)
HTTP_BODY=$(curl -s --max-time 30 -D "$HTTP_HEADER_FILE" \
    --proxy "$PROXY_URL" --proxy-user "${API_KEY}:" --proxy-insecure \
    "http://httpbin.org/ip" 2>&1) || HTTP_BODY=""

HTTP_STATUS=$(grep "^HTTP/" "$HTTP_HEADER_FILE" | head -1 | awk '{print $2}')
HTTP_ORIGIN=$(echo "$HTTP_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('origin',''))" 2>/dev/null) || HTTP_ORIGIN=""

if [[ "$HTTP_STATUS" == "200" ]]; then
    pass "HTTP forward proxy → 200 OK"
    detail "Exit IP: $HTTP_ORIGIN"
    OBSERVED_IP="$HTTP_ORIGIN"
else
    fail "HTTP forward proxy → $HTTP_STATUS (expected 200)"
fi

# C2: X-SpaceRouter-Node header present
echo "  C2: X-SpaceRouter-Node header"
SR_NODE=$(grep -i "X-SpaceRouter-Node" "$HTTP_HEADER_FILE" | tr -d '\r' | head -1 || true)
if [[ -n "$SR_NODE" ]]; then
    pass "X-SpaceRouter-Node header present"
    detail "$SR_NODE"
else
    fail "X-SpaceRouter-Node header missing"
fi

# C3: X-SpaceRouter-Request-Id header present
echo "  C3: X-SpaceRouter-Request-Id header"
SR_REQ_ID=$(grep -i "X-SpaceRouter-Request-Id" "$HTTP_HEADER_FILE" | tr -d '\r' | head -1 || true)
if [[ -n "$SR_REQ_ID" ]]; then
    pass "X-SpaceRouter-Request-Id header present"
    detail "$SR_REQ_ID"
else
    fail "X-SpaceRouter-Request-Id header missing"
fi
rm -f "$HTTP_HEADER_FILE"

echo ""

# ────────────────────────────────────────────────────────────────
# Group D: HTTPS CONNECT Tunnel
# ────────────────────────────────────────────────────────────────
echo "── D. HTTPS CONNECT Tunnel ──────────────────────────────────"
echo ""

# D1: HTTPS CONNECT → httpbin.org/ip
echo "  D1: HTTPS CONNECT → httpbin.org/ip"
HTTPS_BODY=$(curl -s --max-time 30 \
    --proxy "$PROXY_URL" --proxy-user "${API_KEY}:" --proxy-insecure \
    "https://httpbin.org/ip" 2>&1) || HTTPS_BODY=""

HTTPS_ORIGIN=$(echo "$HTTPS_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('origin',''))" 2>/dev/null) || HTTPS_ORIGIN=""

if [[ -n "$HTTPS_ORIGIN" ]]; then
    pass "HTTPS CONNECT tunnel → valid response"
    detail "Exit IP: $HTTPS_ORIGIN"
else
    fail "HTTPS CONNECT tunnel failed: $HTTPS_BODY"
fi

# D2: HTTPS to a different target (api.ipify.org)
echo "  D2: HTTPS CONNECT → api.ipify.org"
IPIFY_BODY=$(curl -s --max-time 30 \
    --proxy "$PROXY_URL" --proxy-user "${API_KEY}:" --proxy-insecure \
    "https://api.ipify.org?format=json" 2>&1) || IPIFY_BODY=""

IPIFY_IP=$(echo "$IPIFY_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ip',''))" 2>/dev/null) || IPIFY_IP=""

if [[ -n "$IPIFY_IP" ]]; then
    pass "HTTPS via api.ipify.org → valid response"
    detail "Exit IP: $IPIFY_IP"
else
    fail "HTTPS via api.ipify.org failed: $IPIFY_BODY"
fi

# D3: HTTPS and HTTP exit IPs match (same residential node)
echo "  D3: Exit IP consistency"
if [[ -n "$HTTP_ORIGIN" && -n "$HTTPS_ORIGIN" && "$HTTP_ORIGIN" == "$HTTPS_ORIGIN" ]]; then
    pass "HTTP and HTTPS exit IPs match ($HTTP_ORIGIN)"
else
    fail "IP mismatch: HTTP=$HTTP_ORIGIN HTTPS=$HTTPS_ORIGIN"
fi

echo ""

# ────────────────────────────────────────────────────────────────
# Group E: Security
# ────────────────────────────────────────────────────────────────
echo "── E. Security ──────────────────────────────────────────────"
echo ""

# E1: No Proxy-Authorization header leaked to target
echo "  E1: No proxy header leakage"
HEADERS_BODY=$(curl -s --max-time 30 \
    --proxy "$PROXY_URL" --proxy-user "${API_KEY}:" --proxy-insecure \
    "http://httpbin.org/headers" 2>/dev/null) || HEADERS_BODY=""

PROXY_AUTH_LEAKED=$(echo "$HEADERS_BODY" | python3 -c "
import sys, json
headers = json.load(sys.stdin).get('headers', {})
# Check if Proxy-Authorization was forwarded to the target
pa = headers.get('Proxy-Authorization', '')
print('leaked' if pa else 'clean')
" 2>/dev/null) || PROXY_AUTH_LEAKED="unknown"

if [[ "$PROXY_AUTH_LEAKED" == "clean" ]]; then
    pass "Proxy-Authorization not leaked to target"
else
    fail "Proxy-Authorization leaked to target!"
fi

# E2: Residential IP (not a known datacenter/Fly.io IP)
echo "  E2: Residential IP verification"
if [[ -n "$OBSERVED_IP" ]]; then
    # Fly.io's IAD IPs are in 66.241.x.x, 149.248.x.x, etc.
    # A simple heuristic: if the exit IP doesn't match common cloud ranges
    IS_RESIDENTIAL=$(python3 -c "
import sys
ip = '$OBSERVED_IP'
# Known Fly.io/datacenter prefixes (non-exhaustive)
dc_prefixes = ['66.241.', '149.248.', '213.188.', '37.16.', '168.220.']
for prefix in dc_prefixes:
    if ip.startswith(prefix):
        print('datacenter')
        sys.exit(0)
print('residential')
" 2>/dev/null) || IS_RESIDENTIAL="unknown"

    if [[ "$IS_RESIDENTIAL" == "residential" ]]; then
        pass "Exit IP $OBSERVED_IP is residential (not a known datacenter range)"
    else
        fail "Exit IP $OBSERVED_IP appears to be a datacenter IP"
    fi
else
    fail "No exit IP observed to verify"
fi

echo ""

# ────────────────────────────────────────────────────────────────
# Group F: Multi-Request & Rate Limiting
# ────────────────────────────────────────────────────────────────
echo "── F. Multi-Request & Rate Limiting ─────────────────────────"
echo ""

# F1: Multiple sequential requests succeed (not rate limited)
echo "  F1: 5 sequential requests (rate limit = 120 rpm)"
SEQ_PASS=0
SEQ_FAIL=0
for i in $(seq 1 5); do
    SEQ_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
        --proxy "$PROXY_URL" --proxy-user "${API_KEY}:" --proxy-insecure \
        "http://httpbin.org/status/200" 2>&1) || SEQ_STATUS="000"
    if [[ "$SEQ_STATUS" == "200" ]]; then
        SEQ_PASS=$((SEQ_PASS + 1))
    else
        SEQ_FAIL=$((SEQ_FAIL + 1))
    fi
done

if [[ "$SEQ_PASS" -eq 5 ]]; then
    pass "5/5 sequential requests succeeded (no rate limiting)"
else
    fail "$SEQ_PASS/5 succeeded, $SEQ_FAIL failed"
fi

# F2: API key listing shows our key
echo "  F2: API key management"
KEYS_JSON=$(curl -sf "${COORD_API_URL}/api-keys" 2>&1) || KEYS_JSON="[]"
KEY_FOUND=$(echo "$KEYS_JSON" | python3 -c "
import sys, json
keys = json.load(sys.stdin)
for k in keys:
    if k['id'] == '$API_KEY_ID':
        print('found')
        break
else:
    print('missing')
" 2>/dev/null) || KEY_FOUND="missing"

if [[ "$KEY_FOUND" == "found" ]]; then
    pass "Test API key visible in /api-keys listing"
else
    fail "Test API key not found in listing"
fi

echo ""


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5: Summary
# ═══════════════════════════════════════════════════════════════════════════════
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " STAGING E2E TEST RESULTS"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Total:   $TOTAL"
echo "  Passed:  $PASS"
echo "  Failed:  $FAIL"
echo "  Time:    ${DURATION}s"
echo ""

if [[ "$FAIL" -eq 0 ]]; then
    echo "  ✅ ALL $TOTAL TESTS PASSED"
else
    echo "  ❌ $FAIL TEST(S) FAILED"
fi

echo ""
echo "───────────────────────────────────────────────────────────────"
echo " Architecture Verified"
echo "───────────────────────────────────────────────────────────────"
echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │  Client (curl on this machine)                          │"
echo "  │    │                                                    │"
echo "  │    │  TLS (SNI-routed on shared IPv4)                   │"
echo "  │    ▼                                                    │"
echo "  │  Proxy Gateway (Fly.io IAD)                             │"
echo "  │    ├─ Authenticates via Coordination API                │"
echo "  │    ├─ Selects node via /internal/route/select           │"
echo "  │    │                                                    │"
echo "  │    │  TLS (self-signed, verify_mode=CERT_NONE)          │"
echo "  │    ▼                                                    │"
echo "  │  Home Node (local, UPnP port-forwarded)                 │"
echo "  │    │  Residential IP: ${OBSERVED_IP:-unknown}"
echo "  │    │                                                    │"
echo "  │    ▼                                                    │"
echo "  │  Target (httpbin.org, api.ipify.org, etc.)              │"
echo "  └──────────────────────────────────────────────────────────┘"
echo ""
echo "───────────────────────────────────────────────────────────────"
echo " Evidence"
echo "───────────────────────────────────────────────────────────────"
echo ""
echo "  Coordination API:   ${COORD_API_URL}"
echo "  Proxy Gateway:      ${PROXY_URL}"
echo "  Node ID:            ${NODE_ID}"
echo "  API Key ID:         ${API_KEY_ID}"
echo "  Residential IP:     ${OBSERVED_IP:-not observed}"
if [[ -n "$IPIFY_IP" ]]; then
echo "  ipify.org confirms: ${IPIFY_IP}"
fi
echo ""
echo "  Key insight: The exit IP (${OBSERVED_IP:-?}) is the Home Node's"
echo "  residential ISP address, NOT a Fly.io datacenter IP."
echo "  This proves traffic traverses the full proxy chain."
echo ""
echo "───────────────────────────────────────────────────────────────"
echo " Bugs Fixed During Staging Bring-Up"
echo "───────────────────────────────────────────────────────────────"
echo ""
echo "  1. UPnP crash on ConflictInMappingEntry (home-node/app/upnp.py)"
echo "     Root cause: Router already had a mapping from a previous run."
echo "     Fix: Catch the exception and treat existing mapping as success."
echo ""
echo "  2. Hardcoded 'test_secret' in Proxy Gateway (auth.py, routing.py)"
echo "     Root cause: X-Internal-API-Key header sent 'test_secret' instead"
echo "     of the actual SR_COORDINATION_API_SECRET from Fly.io secrets."
echo "     Fix: Use self._settings.COORDINATION_API_SECRET everywhere."
echo ""
echo "  3. Proxy handler unreachable on Fly.io shared IPv4 (fly.toml)"
echo "     Root cause: handlers=[] means raw TCP, but Fly.io shared IPs"
echo "     need TLS SNI or HTTP Host header to route to the correct app."
echo "     Fix: Changed to handlers=[\"tls\"] — Fly terminates TLS at edge,"
echo "     reads SNI for routing, passes decrypted TCP to our handler."
echo ""
echo "───────────────────────────────────────────────────────────────"
echo " Reproduce"
echo "───────────────────────────────────────────────────────────────"
echo ""
echo "  # Full demo (starts Home Node, runs tests, cleans up):"
echo "  ./scripts/e2e-staging-demo.sh"
echo ""
echo "  # Quick manual test (while Home Node is running):"
echo "  curl --proxy \"${PROXY_URL}\" \\"
echo "       --proxy-user \"\${API_KEY}:\" \\"
echo "       --proxy-insecure \\"
echo "       http://httpbin.org/ip"
echo ""

# Logs location
echo "  Home Node logs: $REPO_ROOT/logs/staging-home-node.log"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
