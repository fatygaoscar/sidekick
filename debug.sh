#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
Usage:
  ./debug.sh                                    # inline WSL monitor (GPU, Ollama, Job, Pipeline)
  ./debug.sh monitor                            # same as above
  ./debug.sh export [job_id] [interval_seconds] [base_url]
  ./debug.sh ollama [--gpu] [--interval N]
  ./debug.sh logs [filter_pattern]
  ./debug.sh pipeline [filter_pattern]
  ./debug.sh benchmark [benchmark_args...]
  ./debug.sh benchmark-summary [benchmark_args...]

Examples:
  ./debug.sh                                    # live monitor in current terminal
  ./debug.sh monitor                            # same
  ./debug.sh export                             # monitor latest export job
  ./debug.sh export d447ae99                    # monitor specific job
  ./debug.sh ollama --gpu --interval 1
  ./debug.sh logs                               # tail all logs
  ./debug.sh logs "pipeline\|extraction"       # tail with grep filter
  ./debug.sh pipeline                           # tail [step] timing lines only
  ./debug.sh pipeline "summarize"              # filter pipeline steps by name
  ./debug.sh speakers                           # tail diarization + speaker mapping logs
  ./debug.sh benchmark --runs 2                 # Ollama microbenchmark
  ./debug.sh benchmark-summary                  # full pipeline, latest recording
  ./debug.sh benchmark-summary --models qwen3.5:4b,qwen3.5:9b --contexts 16384,32768
  ./debug.sh benchmark-summary --recording-id abc123 --template working_session --preview
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

cmd_pipeline() {
  local filter="${1:-}"
  if [[ ! -f "data/sidekick.log" ]]; then
    echo "No log file at data/sidekick.log"; exit 1
  fi

  printf "\033[2mWaiting for pipeline steps... (Ctrl+C to stop)\033[0m\n\n"

  # Strip log prefix, parse [step] lines, and pretty-print with colors + alignment
  local awk_prog='
    {
      line = $0
      sub(/^[A-Z]+:[^:]+:/, "", line)
      if (line !~ /^\[step\]/) next
      sub(/^\[step\] /, "", line)

      # Replace " | " with ctrl char to safely split (awk treats | as regex alternation)
      gsub(/ [|] /, "\x01", line)
      n = split(line, f, "\x01")
      name   = f[1]
      status = f[2]
      meta = ""
      for (i = 3; i <= n; i++) meta = meta (meta ? "  " : "") f[i]

      ts = strftime("%H:%M:%S")

      GRN = "\033[32m"; CYN = "\033[36m"
      RED = "\033[31m"; YEL = "\033[33m"; RST = "\033[0m"

      if (status == "done")              { clr = GRN; sym = "✓" }
      else if (status == "error")        { clr = RED; sym = "✗" }
      else if (status ~ /^chunk [0-9]/)  { clr = YEL; sym = "·"; sub(/^chunk /, "", status); meta = status; status = "chunk" }
      else                               { clr = CYN; sym = "→" }

      printf "%s%s  %-24s %s %-7s  %s%s\n", clr, ts, name, sym, status, meta, RST
    }
  '

  if [[ -n "$filter" ]]; then
    tail -f "data/sidekick.log" \
      | grep --line-buffered -iE "\\[step\\].*${filter}" \
      | awk "$awk_prog"
  else
    tail -f "data/sidekick.log" | awk "$awk_prog"
  fi
}

_venv_python() {
  if [[ -x "./venv/bin/python3" ]]; then
    echo "./venv/bin/python3"
  else
    echo "python3"
  fi
}

cmd_benchmark() {
  "$(_venv_python)" ./scripts/benchmark_ollama_models.py "$@"
}

cmd_speakers() {
  if [[ ! -f "data/sidekick.log" ]]; then
    echo "No log file at data/sidekick.log"; exit 1
  fi
  printf "\033[2mWatching diarization + speaker mapping logs... (Ctrl+C to stop)\033[0m\n\n"
  tail -f "data/sidekick.log" \
    | grep --line-buffered -iE "diariz|speaker_prepass|speaker_map|resolve_speaker|cohesive: speaker" \
    | sed 's/^[A-Z]*:[^:]*:[^:]*: *//'
}

cmd_benchmark_summary() {
  "$(_venv_python)" ./scripts/benchmark_summary.py "$@"
}

cmd_default() {
  bash "$PWD/scripts/monitor_sidekick.sh"
}

if [[ $# -lt 1 ]]; then
  cmd_default; exit 0
fi

cmd="$1"; shift

case "$cmd" in
  monitor)            cmd_default ;;
  export)             cmd_export "$@" ;;
  ollama)             cmd_ollama "$@" ;;
  logs)               cmd_logs "$@" ;;
  pipeline)           cmd_pipeline "$@" ;;
  speakers)           cmd_speakers "$@" ;;
  benchmark)          cmd_benchmark "$@" ;;
  benchmark-summary)  cmd_benchmark_summary "$@" ;;
  *) echo "Unknown command: $cmd"; usage; exit 1 ;;
esac
