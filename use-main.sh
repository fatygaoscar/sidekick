#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
./scripts/switch_sidekick_branch.sh main "$@"
