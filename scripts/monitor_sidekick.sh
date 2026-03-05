#!/usr/bin/env bash
# Sidekick live monitor — Whisper · Ollama · GPU · Export Job · Pipeline
set -uo pipefail
cd "$(dirname "$0")/.."

BASE_URL="http://127.0.0.1:8000"
INTERVAL=2
LOG="data/sidekick.log"
ENV_FILE=".env"

# ── ANSI ──────────────────────────────────────────────────────────────────────
RST='\033[0m'; BOLD='\033[1m'; DIM='\033[2m'
RED='\033[31m'; GRN='\033[32m'; YEL='\033[33m'; CYN='\033[36m'; WHT='\033[97m'

_env_val() {
  [[ -f "$ENV_FILE" ]] && grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- | tr -d '"' || echo ""
}

# ── Bars ──────────────────────────────────────────────────────────────────────
# Resource bar: green/yellow/red (high = bad)
_bar_res() {
  local pct=$1 width=28 filled=$(( $1 * 28 / 100 ))
  local color; [[ $pct -ge 90 ]] && color="$RED" || { [[ $pct -ge 75 ]] && color="$YEL" || color="$GRN"; }
  local bar="" i; for ((i=0; i<width; i++)); do [[ $i -lt $filled ]] && bar+="█" || bar+="░"; done
  printf "${color}%s${RST} %3d%%" "$bar" "$pct"
}

# Progress bar: cyan (high = good)
_bar_prog() {
  local pct=$1 width=28 filled=$(( $1 * 28 / 100 ))
  local bar="" i; for ((i=0; i<width; i++)); do [[ $i -lt $filled ]] && bar+="█" || bar+="░"; done
  printf "${CYN}%s${RST} %3d%%" "$bar" "$pct"
}

_lbl()    { printf "  ${DIM}%-14s${RST}" "$1"; }   # 2-space indent + fixed-width label
_subsep() { printf "\n  ${DIM}── %s${RST}\n" "$1"; }  # sub-section divider inside a panel

_sep() {
  local title="$1"
  local pad=$(( TERM_W - ${#title} - 4 ))
  [[ $pad -lt 1 ]] && pad=1
  printf "${CYN}╭─${BOLD}${WHT} %s ${RST}${CYN}%s${RST}\n" \
    "$title" "$(printf '─%.0s' $(seq 1 $pad))"
}

_footer() {
  printf "\033[${LINES};1H${DIM}  Ctrl+C stop   ↻ ${INTERVAL}s${RST}\033[K"
}

_find_latest_job_id() {
  [[ -f "$LOG" ]] || return 1
  grep -aoE '/api/export-jobs/[0-9a-f-]+' "$LOG" 2>/dev/null | awk -F/ '{print $NF}' | tail -n 1
}

# Returns the log line number where the last job's pipeline steps begin.
# Anchors on the last summarize_pass1 | start, then walks back to include
# transcription + diarization if they belong to the same job (no intervening
# summarize_pass1 | start between them and the anchor).
_job_start_line() {
  [[ -f "$LOG" ]] || { echo 1; return; }
  local sum1
  sum1="$(grep -anF '[step] summarize_pass1 | start' "$LOG" | tail -n 1 | cut -d: -f1)"
  [[ -z "$sum1" ]] && { echo 1; return; }

  # Last transcription | start that occurred before sum1
  local tx
  tx="$(grep -anF '[step] transcription | start' "$LOG" | \
    awk -F: -v lim="$sum1" '$1+0 < lim+0 { v=$1+0 } END { if (v) print v }')"
  [[ -z "$tx" ]] && { echo "$sum1"; return; }

  # If another summarize_pass1 | start exists between tx and sum1, tx belongs
  # to an older job — use sum1 as the boundary instead
  local between
  between="$(awk -v a="$tx" -v b="$sum1" \
    'NR > a+0 && NR < b+0 && index($0,"[step] summarize_pass1 | start") > 0 \
     { found=1; exit } END { print found+0 }' "$LOG")"
  [[ "${between:-0}" -eq 1 ]] && echo "$sum1" || echo "$tx"
}

# ── Step active? Compare log line numbers ─────────────────────────────────────
# Returns 0 if step named $1 is currently running (start > done in log)
_step_running() {
  local name="$1"
  [[ -f "$LOG" ]] || return 1
  local s d
  s="$(grep -anF "[step] $name | start" "$LOG" | tail -n 1 | cut -d: -f1)"
  d="$(grep -anF "[step] $name | done"  "$LOG" | tail -n 1 | cut -d: -f1)"
  [[ -n "$s" && ( -z "$d" || "$s" -gt "$d" ) ]]
}

# ── GPU ───────────────────────────────────────────────────────────────────────
_gpu() {
  if ! command -v nvidia-smi &>/dev/null; then
    printf "$(_lbl 'Status')${DIM}nvidia-smi not found${RST}\n"; return
  fi
  local raw; raw="$(nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total \
    --format=csv,noheader,nounits 2>/dev/null || true)"
  if [[ -z "$raw" ]]; then printf "$(_lbl 'Status')${DIM}unavailable${RST}\n"; return; fi
  local name util mu mt; IFS=',' read -r name util mu mt <<< "$raw"
  name="${name## }"; name="${name%% }"; util="${util// /}"; mu="${mu// /}"; mt="${mt// /}"
  local mp=$(( mu * 100 / (mt > 0 ? mt : 1) ))
  printf "$(_lbl 'Card')${BOLD}%s${RST}\n" "$name"
  printf "$(_lbl 'Util')%s\n"              "$(_bar_res "$util")"
  printf "$(_lbl 'VRAM')%s  ${DIM}%s / %s MiB${RST}\n" "$(_bar_res "$mp")" "$mu" "$mt"
}

# ── WHISPER ───────────────────────────────────────────────────────────────────
_whisper() {
  local model device compute diarization
  model="$(_env_val WHISPER_MODEL_SIZE)"; device="$(_env_val WHISPER_DEVICE)"
  compute="$(_env_val WHISPER_COMPUTE_TYPE)"; diarization="$(_env_val DIARIZATION_ENABLED)"
  printf "$(_lbl 'Model')${BOLD}whisper/%s${RST}  ${DIM}%s %s  diarization=%s${RST}\n" \
    "${model:-?}" "${device:-?}" "${compute:-?}" "${diarization:-?}"
}

# ── OLLAMA / LLM ─────────────────────────────────────────────────────────────
_ollama() {
  local model_cfg; model_cfg="$(_env_val OLLAMA_MODEL)"
  if command -v ollama &>/dev/null; then
    local ps_out; ps_out="$(ollama ps 2>/dev/null | tail -n +2 || true)"
    if [[ -n "$ps_out" ]]; then
      while IFS= read -r line; do
        local m size proc
        m="$(awk '{print $1}' <<< "$line")"
        size="$(awk '{print $3, $4}' <<< "$line")"
        proc="$(echo "$line" | grep -oE '[0-9]+% (GPU|CPU)' || echo "-")"
        printf "$(_lbl 'Loaded')${BOLD}${CYN}%s${RST}  ${DIM}%s  %s${RST}\n" "$m" "$size" "$proc"
      done <<< "$ps_out"
    else
      printf "$(_lbl 'Model')${DIM}%s${RST}  ${DIM}(not loaded)${RST}\n" "${model_cfg:-?}"
    fi
  else
    printf "$(_lbl 'Model')${DIM}%s${RST}\n" "${model_cfg:-?}"
  fi
}

# ── EXPORT JOB ────────────────────────────────────────────────────────────────
_job() {
  local job_id; job_id="$(_find_latest_job_id 2>/dev/null || true)"
  if [[ -z "${job_id:-}" ]]; then
    _sep 'LAST EXPORT JOB'
    printf "$(_lbl 'Job')${DIM}no export job in log${RST}\n"; return
  fi
  if ! command -v jq &>/dev/null; then
    _sep 'LAST EXPORT JOB'
    printf "$(_lbl 'Job')${DIM}jq not installed${RST}\n"; return
  fi

  local raw; raw="$(curl -sS --max-time 1 "${BASE_URL}/api/export-jobs/${job_id}" 2>/dev/null || true)"
  if [[ -z "$raw" ]]; then
    _sep 'LAST EXPORT JOB'
    printf "$(_lbl 'Job')${DIM}%.8s…  server not responding${RST}\n" "$job_id"; return
  fi

  local fields
  fields="$(printf '%s' "$raw" | jq -r '
    [.status//"-", .stage//"-", (.message//""),
     ((.transcription_progress//0)*100|floor|tostring),
     ((.summarization_progress//0)*100|floor|tostring),
     ((.overall_progress//0)*100|floor|tostring),
     (.updated_at//"")]|join("\n")' 2>/dev/null)" || fields=$'-\n-\n\n0\n0\n0\n'

  local -a f; mapfile -t f <<< "$fields"
  local status="${f[0]}" stage="${f[1]}" msg="${f[2]}" tp="${f[3]:-0}" sp="${f[4]:-0}" op="${f[5]:-0}"
  local ts="${f[6]:-}"; ts="${ts#*T}"; ts="${ts%%.*}"

  if [[ "$status" == "running" || "$status" == "queued" ]]; then
    _sep 'CURRENT EXPORT JOB'
  else
    _sep 'LAST EXPORT JOB'
  fi

  local sc
  case "$status" in
    completed) sc="${GRN}${BOLD}✓ completed${RST}" ;;
    failed)    sc="${RED}${BOLD}✗ failed${RST}" ;;
    running)   sc="${YEL}▶ running${RST}" ;;
    queued)    sc="${DIM}queued${RST}" ;;
    *)         sc="${DIM}$status${RST}" ;;
  esac

  # Fetch recording title from recordings API using session_id (same source as view modal)
  local session_id; session_id="$(printf '%s' "$raw" | jq -r '.session_id // ""' 2>/dev/null || true)"
  local title=""
  if [[ -n "$session_id" ]]; then
    local rec_raw; rec_raw="$(curl -sS --max-time 1 "${BASE_URL}/api/recordings/${session_id}" 2>/dev/null || true)"
    title="$(printf '%s' "$rec_raw" | jq -r '.title // ""' 2>/dev/null || true)"
  fi

  printf "$(_lbl 'Job')${DIM}%s${RST}  %b  ${DIM}%s${RST}\n" "$job_id" "$sc" "$ts"
  [[ -n "$title" ]] && printf "$(_lbl 'Name')${BOLD}%s${RST}\n" "$title"
  printf "$(_lbl 'Stage')${BOLD}%s${RST}  ${DIM}%.45s${RST}\n" "$stage" "$msg"
  printf "$(_lbl 'Transcript')%s\n"  "$(_bar_prog "$tp")"
  printf "$(_lbl 'Summary')%s\n"     "$(_bar_prog "$sp")"
  printf "$(_lbl 'Overall')%s\n"     "$(_bar_prog "$op")"

  if [[ "$status" == "failed" ]]; then
    local err; err="$(printf '%s' "$raw" | jq -r '.error // ""' 2>/dev/null || true)"
    [[ -n "$err" ]] && printf "$(_lbl 'Error')${RED}%.55s${RST}\n" "$err"
  fi

  [[ -f "$LOG" ]] || return
  local job_start="${1:-1}"
  local _log; _log() { tail -n +"$job_start" "$LOG"; }  # helper: log scoped to this job

  # ── Transcription subsection ────────────────────────────────────────────────
  _subsep 'Transcription'
  if _step_running "transcription"; then
    printf "$(_lbl 'Status')${YEL}${BOLD}▶ transcribing...${RST}\n"
  fi
  local tx_real tx_any
  tx_real="$(_log | grep -aF '[step] transcription | done' | \
    awk -F'elapsed=' 'NF>1 { v=$2+0; if(v>1) print }' | tail -n 1 || true)"
  tx_any="$(_log | grep -aF '[step] transcription | done' | tail -n 1 || true)"
  if [[ -n "$tx_real" ]]; then
    local tx_el tx_ch tx_cps
    tx_el="$(echo "$tx_real" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
    tx_ch="$(echo "$tx_real" | grep -oE 'chars=[0-9]+'     | grep -oE '[0-9.]+'  || true)"
    if [[ -n "$tx_el" && -n "$tx_ch" && "${tx_el%.*}" -gt 0 ]]; then
      tx_cps="$(echo "scale=0; $tx_ch / $tx_el" | bc -l 2>/dev/null || echo "?")"
      printf "$(_lbl 'Duration')${GRN}%ss${RST}  ${DIM}%s chars  (~%s chars/s)${RST}\n" "$tx_el" "$tx_ch" "$tx_cps"
    else
      printf "$(_lbl 'Duration')${GRN}%ss${RST}\n" "${tx_el:-?}"
    fi
  elif [[ -n "$tx_any" ]]; then
    # Done step found but elapsed ~0s — transcript was reused from a prior run
    printf "$(_lbl 'Reused')${DIM}cached from prior run${RST}\n"
  else
    printf "$(_lbl 'Duration')${DIM}—${RST}\n"
  fi

  # ── Diarization subsection ──────────────────────────────────────────────────
  _subsep 'Diarization'
  if _step_running "diarization"; then
    printf "$(_lbl 'Status')${YEL}${BOLD}▶ diarizing...${RST}\n"
  fi
  local dz; dz="$(_log | grep -aF '[step] diarization | done' | tail -n 1 || true)"
  if [[ -n "$dz" ]]; then
    local dz_el dz_sp
    dz_el="$(echo "$dz" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
    dz_sp="$(echo "$dz" | grep -oE 'spans=[0-9]+'     | grep -oE '[0-9.]+'  || true)"
    [[ -n "$dz_el" ]] && printf "$(_lbl 'Duration')${GRN}%ss${RST}\n" "$dz_el"
    [[ -n "$dz_sp" ]] && printf "$(_lbl 'Speakers')${DIM}%s${RST}\n"  "$dz_sp"
  else
    printf "$(_lbl 'Duration')${DIM}—${RST}\n"
  fi

  # ── Summarization subsection ────────────────────────────────────────────────
  _subsep 'Summarization'
  if _step_running "summarize_pass1" || _step_running "summarize_pass2"; then
    printf "$(_lbl 'Status')${YEL}${BOLD}▶ summarizing...${RST}\n"
  fi
  local sum_done; sum_done="$(_log | grep -aF '[step] summarization | done' | tail -n 1 || true)"
  if [[ -n "$sum_done" ]]; then
    local sum_el
    sum_el="$(echo "$sum_done" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
    [[ -n "$sum_el" ]] && printf "$(_lbl 'Duration')${GRN}%ss${RST}\n" "$sum_el"
  else
    printf "$(_lbl 'Duration')${DIM}—${RST}\n"
  fi
  local p1 p2 p1_el p2_el
  p1="$(_log | grep -aF '[step] summarize_pass1 | done' | tail -n 1 || true)"
  p2="$(_log | grep -aF '[step] summarize_pass2 | done' | tail -n 1 || true)"
  p1_el="$(echo "$p1" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
  p2_el="$(echo "$p2" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
  if [[ -n "$p1_el" || -n "$p2_el" ]]; then
    local passes=""
    [[ -n "$p1_el" ]] && passes+="pass1 ${p1_el}s"
    [[ -n "$p1_el" && -n "$p2_el" ]] && passes+="  "
    [[ -n "$p2_el" ]] && passes+="pass2 ${p2_el}s"
    printf "$(_lbl 'Passes')${DIM}%s${RST}\n" "$passes"
  fi
  local sum_chars; sum_chars="$(echo "$p1" | grep -oE 'chars=[0-9]+' | grep -oE '[0-9.]+' || true)"
  [[ -n "$sum_chars" ]] && printf "$(_lbl 'Length')${DIM}%s chars${RST}\n" "$sum_chars"
  local tok_s; tok_s="$(_log | grep -aoE '[0-9.]+ tok/s' | tail -n 1 || true)"
  [[ -n "$tok_s" ]] && printf "$(_lbl 'Speed')${GRN}${BOLD}%s${RST}\n" "$tok_s"
  local ctx_mode; ctx_mode="$(_log | grep -aF '[step] summarize_pass1 | start' | \
    tail -n 1 | grep -oE 'mode=[a-z_]+' | cut -d= -f2 || true)"
  [[ -n "$ctx_mode" ]] && printf "$(_lbl 'Context')${DIM}%s${RST}\n" "$ctx_mode"
}

# ── PIPELINE ─────────────────────────────────────────────────────────────────
_pipeline() {
  local job_start="${1:-1}"
  if [[ ! -f "$LOG" ]]; then printf "$(_lbl 'Status')${DIM}no log${RST}\n"; return; fi
  local lines; lines="$(tail -n +"$job_start" "$LOG" | grep -aF '[step]' | tail -n 15 || true)"
  if [[ -z "$lines" ]]; then printf "$(_lbl 'Status')${DIM}no activity${RST}\n"; return; fi

  while IFS= read -r raw; do
    # Strip log prefix (e.g. "INFO:module:"), then the "[step] " tag
    local line="${raw#*\[step\] }"

    # Split on " | " — same logic as cmd_pipeline awk
    local name status meta rest
    name="${line%% | *}"
    rest="${line#*| }"; rest="${rest# }"
    status="${rest%% | *}"
    meta="${rest#"$status"}"; meta="${meta# | }"

    local clr sym
    # Handle "chunk N" status (matches awk: status ~ /^chunk [0-9]/)
    if [[ "$status" =~ ^chunk\ [0-9] ]]; then
      clr="$YEL"; sym="·"
      meta="${status#chunk }"; status="chunk"
    else
      case "$status" in
        done)  clr="$GRN"; sym="✓" ;;
        error) clr="$RED"; sym="✗" ;;
        *)     clr="$CYN"; sym="→" ;;
      esac
    fi

    printf "${clr}  %-24s %s %-7s  %s${RST}\n" "$name" "$sym" "$status" "$meta"
  done <<< "$lines"
}

# ── Poll for keypress (non-blocking) ─────────────────────────────────────────
_poll_input() {
  local k
  # Drain ALL pending input; discard mouse escape sequences
  while IFS= read -r -s -N1 -t0 k 2>/dev/null; do
    if [[ "$k" == $'\033' ]]; then
      IFS= read -r -s -N64 -t0.05 _ 2>/dev/null || true
    fi
  done
}

# ── Cleanup ───────────────────────────────────────────────────────────────────
_stty_orig=$(stty -g 2>/dev/null || true)
_cleanup() {
  # Disable mouse tracking + restore screen — must go to /dev/tty directly
  printf '\033[?1000l\033[?1006l\033[?1049l\033[?25h' > /dev/tty
  # Drain any mouse events buffered in stdin so they don't leak to the parent shell
  while IFS= read -r -s -N64 -t0.05 _ 2>/dev/null; do :; done
  [[ -n "$_stty_orig" ]] && stty "$_stty_orig" 2>/dev/null || true
}

# ── Tick indicator cycles through chars each redraw ──────────────────────────
_TICK_CHARS=('·' '•' '●' '•')
_TICK_IDX=0

# ── Draw ─────────────────────────────────────────────────────────────────────
printf '\033[?1049h\033[?25l\033[?1000h\033[?1006h'   # enter alt screen, hide cursor, enable mouse reporting
stty -echo cbreak min 0 time 0 2>/dev/null || true
_RESIZE=0
_handle_resize() { _RESIZE=1; }
trap '_cleanup' EXIT
trap 'exit 0' INT TERM HUP
trap '_handle_resize' WINCH

_draw() {
  TERM_W=$(tput cols 2>/dev/null || echo 80)
  LINES=$(tput lines 2>/dev/null || echo 24)

  local tick="${_TICK_CHARS[$_TICK_IDX]}"
  _TICK_IDX=$(( (_TICK_IDX + 1) % ${#_TICK_CHARS[@]} ))

  # Compute job boundary outside the subshell (can't export vars from inside it)
  local job_start; job_start="$(_job_start_line)"

  # Build entire frame in one write to eliminate flicker
  printf '%s' "$(
    printf '\033[3J\033[2J\033[H'
    printf "${BOLD}${WHT}  ⬡  SIDEKICK MONITOR${RST}    ${DIM}$(date +%H:%M:%S)  ↻${INTERVAL}s  ${CYN}${tick}${RST}\n\n"
    _sep 'GPU';                      _gpu;      echo
    _sep 'WHISPER'; _whisper;  echo
    _sep 'OLLAMA';  _ollama;   echo
    _job "$job_start";      echo
    _sep 'PIPELINE'; _pipeline "$job_start"
    _footer
  )"
}

while true; do
  _draw
  _poll_input
  if [[ $_RESIZE -eq 1 ]]; then
    _RESIZE=0
    continue   # redraw immediately on resize, skip sleep
  fi
  sleep "$INTERVAL"
  _poll_input
done
