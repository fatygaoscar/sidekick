#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: $0 <job_id> [interval_seconds] [base_url]"
  echo "Example: $0 d447ae99-96f7-44a2-9d89-1667603ec376 1 http://127.0.0.1:8000"
  exit 1
fi

JOB_ID="$1"
INTERVAL="${2:-1}"
BASE_URL="${3:-http://127.0.0.1:8000}"
URL="${BASE_URL%/}/api/export-jobs/${JOB_ID}"

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required" >&2; exit 1
fi

# ANSI
RED='\033[31m'; GREEN='\033[32m'; CYAN='\033[36m'
BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

_bar() {
  local pct=$1 width=26 i bar=""
  local filled=$(( pct * width / 100 ))
  for ((i=0; i<width; i++)); do
    [[ $i -lt $filled ]] && bar+="█" || bar+="░"
  done
  printf "%s %3d%%" "$bar" "$pct"
}

# Number of lines the status block occupies (must match _draw exactly)
BLOCK_LINES=12
FIRST_DRAW=1

_draw() {
  local raw="$1"

  # Parse all fields with a single jq call
  local fields
  fields="$(printf '%s' "$raw" | jq -r '
    [
      .status // "unknown",
      .stage // "-",
      (.message // "-"),
      ((.transcription_progress // 0) * 100 | floor | tostring),
      ((.summarization_progress // 0) * 100 | floor | tostring),
      ((.overall_progress // 0) * 100 | floor | tostring),
      (.updated_at // "-")
    ] | join("\n")
  ' 2>/dev/null)" || fields=$'unknown\n-\n-\n0\n0\n0\n-'

  local -a f
  mapfile -t f <<< "$fields"

  local status="${f[0]:-unknown}"
  local stage="${f[1]:-}"
  local message="${f[2]:-}"
  local tp="${f[3]:-0}"
  local sp="${f[4]:-0}"
  local op="${f[5]:-0}"
  local ts_raw="${f[6]:-}"

  # Format timestamp: 2026-03-02T14:23:01.123Z → 14:23:01
  local ts="${ts_raw#*T}"   # strip date part
  ts="${ts%%.*}"            # strip fractional seconds

  # Erase previous block
  if [[ $FIRST_DRAW -eq 0 ]]; then
    printf "\033[%dA\033[J" "$BLOCK_LINES"
  fi
  FIRST_DRAW=0

  local status_str
  case "$status" in
    completed) status_str="${GREEN}${BOLD}completed ✓${RESET}" ;;
    failed)    status_str="${RED}${BOLD}failed ✗${RESET}" ;;
    running)   status_str="${CYAN}running${RESET}" ;;
    queued)    status_str="${DIM}queued${RESET}" ;;
    *)         status_str="$status" ;;
  esac

  # Latest tok/s from server log (updated after each LLM call completes)
  local tok_s="-"
  if [[ -f "data/sidekick.log" ]]; then
    local tok_line
    tok_line="$(grep -o '[0-9.]* tok/s' data/sidekick.log 2>/dev/null | tail -1)"
    [[ -n "$tok_line" ]] && tok_s="$tok_line"
  fi

  local hr="────────────────────────────────────────────────"
  printf "%s\n" "$hr"                                                      # 1
  printf " ${BOLD}Status${RESET}   %b\n" "$status_str"                    # 2
  printf " ${BOLD}Stage${RESET}    %s\n" "$stage"                         # 3
  printf " ${BOLD}Message${RESET}  %.65s\n" "$message"                    # 4
  printf "\n"                                                               # 5
  printf " Transcript  %s\n" "$(_bar "$tp")"                               # 6
  printf " Summary     %s\n" "$(_bar "$sp")"                               # 7
  printf " Overall     %s\n" "$(_bar "$op")"                               # 8
  printf "\n"                                                               # 9
  printf " ${DIM}Speed %s  Updated %s${RESET}\n" "$tok_s" "$ts"           # 10
  printf "%s\n" "$hr"                                                      # 11
  printf "\n"                                                               # 12
}

printf "${BOLD}Export${RESET} %.8s…  ${DIM}(Ctrl+C to stop)${RESET}\n\n" "$JOB_ID"

while true; do
  RAW="$(curl -sS "$URL" 2>/dev/null || true)"

  if [[ -z "$RAW" ]]; then
    [[ $FIRST_DRAW -eq 0 ]] && printf "\033[%dA\033[J" "$BLOCK_LINES"
    printf " [%s] No response from %s\n" "$(date -u +%H:%M:%SZ)" "$URL"
    FIRST_DRAW=0; BLOCK_LINES=1
    sleep "$INTERVAL"
    continue
  fi

  _draw "$RAW"

  STATUS="$(printf '%s' "$RAW" | jq -r '.status // "unknown"')"

  if [[ "$STATUS" == "completed" ]]; then
    echo ""
    printf '%s' "$RAW" | jq -r '"  File: \(.result.filename // "-")\n  Path: \(.result.filepath // "-")"' 2>/dev/null || true
    echo ""
    exit 0
  fi

  if [[ "$STATUS" == "failed" ]]; then
    echo ""
    printf '%s' "$RAW" | jq -r '"  Error: \(.error // "unknown")"' 2>/dev/null || true
    echo ""
    exit 1
  fi

  sleep "$INTERVAL"
done
