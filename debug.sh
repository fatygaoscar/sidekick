#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
Usage:
  ./debug.sh export [job_id] [interval_seconds] [base_url]
  ./debug.sh ollama [--gpu] [--interval N]
  ./debug.sh logs [filter_pattern]
  ./debug.sh benchmark [benchmark_args...]

Examples:
  ./debug.sh export                          # monitor latest export job
  ./debug.sh export d447ae99                 # monitor specific job
  ./debug.sh ollama --gpu --interval 1
  ./debug.sh logs                            # tail all logs
  ./debug.sh logs "pipeline\|extraction"    # tail with grep filter
  ./debug.sh benchmark --runs 2
EOF
}

_find_latest_job_id() {
  [[ -f "data/sidekick.log" ]] || return 1
  grep -Eo '/api/export-jobs/[0-9a-f-]+' data/sidekick.log \
    | sed 's#.*/##' \
    | tail -n 1
}

cmd_export() {
  local job_id="${1:-}"
  local interval="${2:-1}"
  local base_url="${3:-http://127.0.0.1:8000}"

  if [[ -z "$job_id" ]]; then
    job_id="$(_find_latest_job_id || true)"
    if [[ -z "${job_id:-}" ]]; then
      echo "No export job found in data/sidekick.log. Provide a job id or start an export first."
      exit 2
    fi
    echo "Using latest job: $job_id"
  fi

  ./scripts/monitor_export_job.sh "$job_id" "$interval" "$base_url"
}

cmd_ollama() {
  local show_gpu=0 interval=2

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --gpu) show_gpu=1; shift ;;
      --interval)
        interval="${2:?Missing value for --interval}"; shift 2 ;;
      *) echo "Unknown ollama option: $1"; usage; exit 1 ;;
    esac
  done

  local script_win
  script_win="$(wslpath -w "$PWD/scripts/monitor_ollama.ps1")"

  if [[ $show_gpu -eq 1 ]]; then
    powershell.exe -ExecutionPolicy Bypass -File "$script_win" -IntervalSeconds "$interval" -ShowGpu
  else
    powershell.exe -ExecutionPolicy Bypass -File "$script_win" -IntervalSeconds "$interval"
  fi
}

cmd_logs() {
  local filter="${1:-}"
  if [[ ! -f "data/sidekick.log" ]]; then
    echo "No log file at data/sidekick.log"; exit 1
  fi
  if [[ -n "$filter" ]]; then
    tail -f "data/sidekick.log" | grep --line-buffered -iE "$filter"
  else
    tail -f "data/sidekick.log"
  fi
}

cmd_benchmark() {
  python3 ./scripts/benchmark_ollama_models.py "$@"
}

if [[ $# -lt 1 ]]; then
  usage; exit 1
fi

cmd="$1"; shift

case "$cmd" in
  export)    cmd_export "$@" ;;
  ollama)    cmd_ollama "$@" ;;
  logs)      cmd_logs "$@" ;;
  benchmark) cmd_benchmark "$@" ;;
  *) echo "Unknown command: $cmd"; usage; exit 1 ;;
esac
