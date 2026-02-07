#!/bin/bash
set -e

cd "$(dirname "$0")"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting Sidekick...${NC}"

# 1. Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# 2. Activate venv
source venv/bin/activate

# 3. Check and install dependencies only if needed
echo -e "${YELLOW}Checking dependencies...${NC}"
pip install -q -e . 2>/dev/null || pip install -e .

# 4. Start Ollama if not running
echo -e "${YELLOW}Checking Ollama...${NC}"
if ! command -v ollama &> /dev/null; then
    echo -e "${RED}Ollama not installed. Install with: curl -fsSL https://ollama.com/install.sh | sudo sh${NC}"
    exit 1
fi

if ! curl -s http://localhost:11434/api/tags &> /dev/null; then
    echo -e "${YELLOW}Starting Ollama...${NC}"
    ollama serve > /tmp/ollama.log 2>&1 &
    OLLAMA_PID=$!
    sleep 2

    # Verify Ollama started
    if ! curl -s http://localhost:11434/api/tags &> /dev/null; then
        echo -e "${RED}Failed to start Ollama. Check /tmp/ollama.log${NC}"
        exit 1
    fi
    echo -e "${GREEN}Ollama started${NC}"
else
    echo -e "${GREEN}Ollama already running${NC}"
    OLLAMA_PID=""
fi

# 5. Start ngrok in background and capture URL
echo -e "${YELLOW}Starting ngrok tunnel...${NC}"
~/.local/bin/ngrok http 8000 --log=stdout > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

# Wait for ngrok to initialize and get the URL
sleep 3
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | grep -oP '"public_url":"https://[^"]+' | grep -oP 'https://[^"]+' | head -1)

if [ -z "$NGROK_URL" ]; then
    echo -e "${YELLOW}Ngrok not available - running locally only${NC}"
    NGROK_URL="http://localhost:8000"
    kill $NGROK_PID 2>/dev/null
    NGROK_PID=""
else
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}Ngrok URL: ${NGROK_URL}${NC}"
    echo -e "${GREEN}========================================${NC}"

    # Copy URL to clipboard if possible (WSL)
    echo "$NGROK_URL" | clip.exe 2>/dev/null && echo -e "${YELLOW}URL copied to clipboard!${NC}"
fi

# Open in default browser (Windows)
cmd.exe /c start "$NGROK_URL" 2>/dev/null || true

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Shutting down...${NC}"
    [ -n "$NGROK_PID" ] && kill $NGROK_PID 2>/dev/null
    [ -n "$OLLAMA_PID" ] && kill $OLLAMA_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

# 6. Run the app (foreground)
echo -e "${GREEN}Starting Sidekick server...${NC}"
echo -e "${GREEN}Local: http://localhost:8000${NC}"
python -m src.main
