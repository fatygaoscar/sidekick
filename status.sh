#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

PID_FILE="data/sidekick.pid"
NGROK_PID_FILE="data/ngrok.pid"
NGROK_URL_FILE="data/ngrok.url"
PORT="${PORT:-8000}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
NGROK_API_PORT="${NGROK_API_PORT:-4040}"

is_running() {
    local pid="$1"
    kill -0 "$pid" 2>/dev/null
}

get_listener_pid() {
    lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

is_sidekick_pid() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return 1
    fi
    local cmd
    cmd="$(ps -p "$pid" -o cmd= 2>/dev/null || true)"
    [[ "$cmd" == *"python -m src.main"* || "$cmd" == *"python3 -m src.main"* ]]
}

read_ngrok_public_url() {
    curl -fsS "http://127.0.0.1:${NGROK_API_PORT}/api/tunnels" 2>/dev/null | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
for t in data.get("tunnels", []):
    u=t.get("public_url", "")
    if u.startswith("https://"):
        print(u)
        raise SystemExit(0)
for t in data.get("tunnels", []):
    u=t.get("public_url", "")
    if u:
        print(u)
        raise SystemExit(0)
print("")' || true
}

# Sidekick status
if [ ! -f "$PID_FILE" ]; then
    LISTENER_PID="$(get_listener_pid)"
    if [ -n "$LISTENER_PID" ] && is_sidekick_pid "$LISTENER_PID"; then
        echo -e "${YELLOW}Sidekick status: running (unmanaged)${NC}"
        echo "PID: ${LISTENER_PID}"
        echo "URL: http://localhost:${PORT}"
        echo "Hint: run ./start.sh once to adopt PID management."
    else
        echo -e "${YELLOW}Sidekick status: stopped (no PID file)${NC}"
    fi
else
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -z "$PID" ]; then
        echo -e "${YELLOW}Sidekick status: stopped (empty PID file)${NC}"
    elif is_running "$PID"; then
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            echo -e "${GREEN}Sidekick status: running${NC}"
        else
            echo -e "${YELLOW}Sidekick status: running (health check unavailable from this shell)${NC}"
        fi
        echo "PID: ${PID}"
        echo "URL: http://localhost:${PORT}"
    else
        echo -e "${RED}Sidekick status: stale PID file (process ${PID} not running)${NC}"
        rm -f "$PID_FILE"
        echo -e "${YELLOW}Removed stale PID file.${NC}"
    fi
fi

# ngrok status
if [ -f "$NGROK_PID_FILE" ]; then
    NGROK_PID="$(cat "$NGROK_PID_FILE" 2>/dev/null || true)"
    if [ -n "$NGROK_PID" ] && is_running "$NGROK_PID"; then
        NGROK_URL="$(cat "$NGROK_URL_FILE" 2>/dev/null || true)"
        if [ -z "$NGROK_URL" ]; then
            NGROK_URL="$(read_ngrok_public_url)"
            [ -n "$NGROK_URL" ] && echo "$NGROK_URL" > "$NGROK_URL_FILE"
        fi
        echo -e "${GREEN}ngrok status: running${NC}"
        echo "PID: ${NGROK_PID}"
        [ -n "$NGROK_URL" ] && echo "Public URL: ${NGROK_URL}"
    else
        echo -e "${YELLOW}ngrok status: stale PID file${NC}"
        rm -f "$NGROK_PID_FILE" "$NGROK_URL_FILE"
    fi
else
    echo -e "${YELLOW}ngrok status: stopped${NC}"
fi
