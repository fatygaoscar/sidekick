#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

PID_FILE="data/sidekick.pid"
LOG_FILE="data/sidekick.log"
NGROK_PID_FILE="data/ngrok.pid"
NGROK_LOG_FILE="data/ngrok.log"
NGROK_URL_FILE="data/ngrok.url"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
NGROK_API_PORT="${NGROK_API_PORT:-4040}"

ENABLE_NGROK=0
for arg in "$@"; do
    case "$arg" in
        --ngrok)
            ENABLE_NGROK=1
            ;;
        --help|-h)
            echo "Usage: ./start.sh [--ngrok]"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown argument: ${arg}${NC}"
            echo "Usage: ./start.sh [--ngrok]"
            exit 1
            ;;
    esac
done

mkdir -p data

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

port_is_available() {
    python3 - "$PORT" <<'PY'
import socket
import sys

port = int(sys.argv[1])
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except PermissionError:
    sys.exit(2)
try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
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

start_ngrok() {
    if [ "$ENABLE_NGROK" -ne 1 ]; then
        return 0
    fi

    if ! command -v ngrok >/dev/null 2>&1; then
        echo -e "${RED}ngrok not found. Install ngrok or run without --ngrok.${NC}"
        return 1
    fi

    if [ -f "$NGROK_PID_FILE" ]; then
        existing_ngrok_pid="$(cat "$NGROK_PID_FILE" 2>/dev/null || true)"
        if [ -n "$existing_ngrok_pid" ] && is_running "$existing_ngrok_pid"; then
            ngrok_url="$(cat "$NGROK_URL_FILE" 2>/dev/null || true)"
            if [ -z "$ngrok_url" ]; then
                ngrok_url="$(read_ngrok_public_url)"
                [ -n "$ngrok_url" ] && echo "$ngrok_url" > "$NGROK_URL_FILE"
            fi
            echo -e "${GREEN}ngrok already running (PID ${existing_ngrok_pid})${NC}"
            [ -n "$ngrok_url" ] && echo -e "${GREEN}Public URL: ${ngrok_url}${NC}"
            return 0
        fi
        rm -f "$NGROK_PID_FILE" "$NGROK_URL_FILE"
    fi

    echo -e "${YELLOW}Starting ngrok tunnel...${NC}"
    : > "$NGROK_LOG_FILE"
    nohup ngrok http "http://localhost:${PORT}" --log=stdout >> "$NGROK_LOG_FILE" 2>&1 &
    ngrok_pid=$!
    echo "$ngrok_pid" > "$NGROK_PID_FILE"

    for _ in $(seq 1 20); do
        if ! is_running "$ngrok_pid"; then
            echo -e "${RED}ngrok exited unexpectedly.${NC}"
            rm -f "$NGROK_PID_FILE" "$NGROK_URL_FILE"
            tail -n 40 "$NGROK_LOG_FILE" || true
            return 1
        fi

        ngrok_url="$(read_ngrok_public_url)"
        if [ -n "$ngrok_url" ]; then
            echo "$ngrok_url" > "$NGROK_URL_FILE"
            echo -e "${GREEN}Public URL: ${ngrok_url}${NC}"
            return 0
        fi

        sleep 1
    done

    echo -e "${YELLOW}ngrok started but URL not ready yet. Check: tail -f ${NGROK_LOG_FILE}${NC}"
    return 0
}

adopt_existing_sidekick() {
    LISTENER_PID="$(get_listener_pid)"
    if [ -n "$LISTENER_PID" ] && is_sidekick_pid "$LISTENER_PID"; then
        echo "$LISTENER_PID" > "$PID_FILE"
        echo -e "${YELLOW}Sidekick is already running on port ${PORT} (PID ${LISTENER_PID}).${NC}"
        echo -e "${YELLOW}Adopted existing process into ${PID_FILE}.${NC}"
        start_ngrok || true
        exit 0
    fi
}

if [ -f "$PID_FILE" ]; then
    EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${EXISTING_PID}" ] && is_running "$EXISTING_PID"; then
        echo -e "${YELLOW}Sidekick is already running (PID ${EXISTING_PID})${NC}"
        start_ngrok || true
        echo -e "${YELLOW}Status: ./status.sh${NC}"
        echo -e "${YELLOW}Stop:   ./stop.sh${NC}"
        exit 0
    fi
    echo -e "${YELLOW}Removing stale PID file${NC}"
    rm -f "$PID_FILE"
fi

adopt_existing_sidekick

set +e
port_is_available
PORT_CHECK_RC=$?
set -e

if [ "$PORT_CHECK_RC" -eq 1 ]; then
    LISTENER_PID="$(get_listener_pid)"
    if [ -n "$LISTENER_PID" ]; then
        LISTENER_CMD="$(ps -p "$LISTENER_PID" -o cmd= 2>/dev/null || true)"
        if [ -n "$LISTENER_CMD" ]; then
            echo -e "${YELLOW}Current listener PID ${LISTENER_PID}: ${LISTENER_CMD}${NC}"
        fi
    fi
    echo -e "${RED}Port ${PORT} is already in use by another process.${NC}"
    echo -e "${YELLOW}If it is Sidekick from an older run, run ./stop.sh to terminate it.${NC}"
    echo -e "${YELLOW}Try: lsof -nP -iTCP:${PORT} -sTCP:LISTEN${NC}"
    exit 1
elif [ "$PORT_CHECK_RC" -eq 2 ]; then
    echo -e "${YELLOW}Unable to probe port availability in this environment. Continuing...${NC}"
fi

echo -e "${GREEN}Starting Sidekick...${NC}"

if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

source venv/bin/activate

if ! python -c "import src" >/dev/null 2>&1; then
    echo -e "${YELLOW}Installing dependencies...${NC}"
    pip install -e .
fi

echo -e "${YELLOW}Launching server on ${HOST}:${PORT}...${NC}"
: > "$LOG_FILE"
nohup env HOST="$HOST" PORT="$PORT" python -m src.main >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

for _ in $(seq 1 30); do
    if ! is_running "$NEW_PID"; then
        echo -e "${RED}Server exited unexpectedly during startup.${NC}"
        rm -f "$PID_FILE"
        tail -n 80 "$LOG_FILE" || true
        exit 1
    fi

    if grep -q "error while attempting to bind on address" "$LOG_FILE" || grep -q "\[Errno 98\]" "$LOG_FILE"; then
        echo -e "${RED}Server failed to bind to ${HOST}:${PORT}.${NC}"
        rm -f "$PID_FILE"
        tail -n 80 "$LOG_FILE" || true
        exit 1
    fi

    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        echo -e "${GREEN}Sidekick started (PID ${NEW_PID})${NC}"
        echo -e "${GREEN}URL: http://localhost:${PORT}${NC}"
        echo -e "${YELLOW}Logs: tail -f ${LOG_FILE}${NC}"
        start_ngrok || true
        exit 0
    fi

    if grep -q "Application startup complete" "$LOG_FILE"; then
        stable_startup=1
        for _ in 1 2 3; do
            sleep 1
            if ! is_running "$NEW_PID"; then
                stable_startup=0
                break
            fi
        done
        if [ "$stable_startup" -eq 1 ]; then
            echo -e "${GREEN}Sidekick started (PID ${NEW_PID})${NC}"
            echo -e "${GREEN}URL: http://localhost:${PORT}${NC}"
            echo -e "${YELLOW}Logs: tail -f ${LOG_FILE}${NC}"
            start_ngrok || true
            exit 0
        fi
    fi

    sleep 1
done

echo -e "${RED}Server did not become healthy in time.${NC}"
echo -e "${RED}Recent logs:${NC}"
tail -n 80 "$LOG_FILE" || true
exit 1
