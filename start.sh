#!/bin/bash
set -e

cd "$(dirname "$0")"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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
# Compare installed packages against pyproject.toml dependencies
echo -e "${YELLOW}Checking dependencies...${NC}"
pip install -q -e . 2>/dev/null || pip install -e .

# 4. Start ngrok in background and capture URL
echo -e "${YELLOW}Starting ngrok tunnel...${NC}"
~/.local/bin/ngrok http 8000 --log=stdout > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

# Wait for ngrok to initialize and get the URL
sleep 3
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | grep -oP '"public_url":"https://[^"]+' | grep -oP 'https://[^"]+' | head -1)

if [ -z "$NGROK_URL" ]; then
    echo "Failed to get ngrok URL. Check if ngrok is authenticated."
    echo "Run: ~/.local/bin/ngrok config add-authtoken <your-token>"
    kill $NGROK_PID 2>/dev/null
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Ngrok URL: ${NGROK_URL}${NC}"
echo -e "${GREEN}========================================${NC}"

# 5. Copy URL to clipboard if possible (WSL)
echo "$NGROK_URL" | clip.exe 2>/dev/null && echo -e "${YELLOW}URL copied to clipboard!${NC}"

# 6. Open in default browser (Windows)
cmd.exe /c start "$NGROK_URL" 2>/dev/null || true

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Shutting down...${NC}"
    kill $NGROK_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

# 7. Run the app (foreground)
echo -e "${GREEN}Starting Sidekick server...${NC}"
python -m src.main
