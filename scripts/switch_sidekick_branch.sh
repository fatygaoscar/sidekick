#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

TARGET_BRANCH="${1:-}"
if [[ -z "$TARGET_BRANCH" ]]; then
    echo "Usage: ./scripts/switch_sidekick_branch.sh <main|dev> [start.sh args...]"
    exit 1
fi
shift || true

if [[ "$TARGET_BRANCH" != "main" && "$TARGET_BRANCH" != "dev" ]]; then
    echo "Target branch must be 'main' or 'dev'"
    exit 1
fi

STATE_FILE="data/branch-switch-state.env"
AUTO_STASH_PREFIX="sidekick-auto-stash"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() {
    echo -e "${GREEN}$1${NC}"
}

warn() {
    echo -e "${YELLOW}$1${NC}"
}

fail() {
    echo -e "${RED}$1${NC}" >&2
    exit 1
}

has_worktree_changes() {
    if ! git diff --quiet --ignore-submodules --; then
        return 0
    fi
    if ! git diff --cached --quiet --ignore-submodules --; then
        return 0
    fi
    if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
        return 0
    fi
    return 1
}

stash_ref_for_sha() {
    local sha="$1"
    git stash list --format='%gd %H %gs' | awk -v want="$sha" '$2 == want { print $1; exit }'
}

current_branch="$(git branch --show-current)"
if [[ -z "$current_branch" ]]; then
    fail "Could not determine the current git branch."
fi

if [[ "$current_branch" != "main" && "$current_branch" != "dev" ]]; then
    fail "Current branch '$current_branch' is unsupported for this helper. Use dev or main."
fi

mkdir -p data

if [[ "$TARGET_BRANCH" == "main" && -f "$STATE_FILE" ]]; then
    fail "Auto-stash state already exists at $STATE_FILE. Switch back with ./use-dev.sh first."
fi

if [[ "$current_branch" == "main" && "$TARGET_BRANCH" == "dev" ]] && has_worktree_changes; then
    fail "main has local changes. Clean or stash them manually before switching back to dev."
fi

warn "Stopping Sidekick before switching branches..."
./stop.sh

if [[ "$current_branch" == "dev" && "$TARGET_BRANCH" == "main" ]] && has_worktree_changes; then
    stash_message="${AUTO_STASH_PREFIX}:dev-to-main:$(date -Iseconds)"
    warn "Stashing dirty dev worktree..."
    git stash push -u -m "$stash_message" >/dev/null
    stash_sha="$(git rev-parse refs/stash)"
    cat > "$STATE_FILE" <<EOF
SOURCE_BRANCH=dev
TARGET_BRANCH=main
STASH_SHA=$stash_sha
STASH_MESSAGE=$stash_message
EOF
    info "Saved dev changes in auto-stash $stash_sha"
fi

if [[ "$current_branch" != "$TARGET_BRANCH" ]]; then
    warn "Switching to $TARGET_BRANCH..."
    git switch "$TARGET_BRANCH"
else
    info "Already on $TARGET_BRANCH"
fi

if [[ "$TARGET_BRANCH" == "dev" && -f "$STATE_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
    if [[ "${SOURCE_BRANCH:-}" == "dev" && -n "${STASH_SHA:-}" ]]; then
        stash_ref="$(stash_ref_for_sha "$STASH_SHA")"
        if [[ -z "$stash_ref" ]]; then
            fail "Auto-stash $STASH_SHA was not found. Inspect git stash list before proceeding."
        fi
        if has_worktree_changes; then
            fail "dev worktree is not clean, so the auto-stash cannot be restored safely."
        fi
        warn "Restoring auto-stashed dev changes..."
        if git stash pop "$stash_ref"; then
            rm -f "$STATE_FILE"
            info "Restored dev worktree changes"
        else
            fail "Auto-stash restore hit conflicts. Resolve them manually; the stash was left intact."
        fi
    fi
fi

warn "Starting Sidekick on $TARGET_BRANCH..."
./start.sh "$@"
