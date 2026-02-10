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
CLOUDFLARE_PID_FILE="data/cloudflare.pid"
CLOUDFLARE_URL_FILE="data/cloudflare.url"
PORT="${PORT:-8000}"

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

stop_pid_graceful() {
    local pid="$1"
    local name="$2"

    if ! is_running "$pid"; then
        return 0
    fi

    echo -e "${YELLOW}Stopping ${name} (PID ${pid})...${NC}"
    kill "$pid" 2>/dev/null || true

    for _ in $(seq 1 10); do
        if ! is_running "$pid"; then
            echo -e "${GREEN}${name} stopped.${NC}"
            return 0
        fi
        sleep 1
    done

    echo -e "${RED}${name} graceful shutdown timed out. Forcing stop...${NC}"
    kill -9 "$pid" 2>/dev/null || true
    echo -e "${GREEN}${name} stopped.${NC}"
}

if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$PID" ]; then
        stop_pid_graceful "$PID" "Sidekick"
    else
        echo -e "${YELLOW}PID file is empty. Removing it.${NC}"
    fi
    rm -f "$PID_FILE"
else
    LISTENER_PID="$(get_listener_pid)"
    if [ -n "$LISTENER_PID" ] && is_sidekick_pid "$LISTENER_PID"; then
        stop_pid_graceful "$LISTENER_PID" "Sidekick"
    else
        echo -e "${YELLOW}No PID file found. Sidekick may not be running.${NC}"
    fi
fi

if [ -f "$NGROK_PID_FILE" ]; then
    NGROK_PID="$(cat "$NGROK_PID_FILE" 2>/dev/null || true)"
    if [ -n "$NGROK_PID" ]; then
        stop_pid_graceful "$NGROK_PID" "ngrok"
    fi
    rm -f "$NGROK_PID_FILE" "$NGROK_URL_FILE"
fi

if [ -f "$CLOUDFLARE_PID_FILE" ]; then
    CLOUDFLARE_PID="$(cat "$CLOUDFLARE_PID_FILE" 2>/dev/null || true)"
    if [ -n "$CLOUDFLARE_PID" ]; then
        stop_pid_graceful "$CLOUDFLARE_PID" "cloudflare tunnel"
    fi
    rm -f "$CLOUDFLARE_PID_FILE" "$CLOUDFLARE_URL_FILE"
fi
