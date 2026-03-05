#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

print_usage() {
    echo "Usage: ./restart.sh [--ngrok|--cloudflare]"
}

for arg in "$@"; do
    case "$arg" in
        --ngrok|--cloudflare)
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown argument: ${arg}${NC}"
            print_usage
            exit 1
            ;;
    esac
done

echo -e "${GREEN}Restarting Sidekick...${NC}"
./stop.sh
./start.sh "$@"
