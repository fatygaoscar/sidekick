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
BG_HDR='\033[48;5;235m'

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

_lbl()  { printf "${DIM}%-14s${RST}" "$1"; }  # fixed-width label
_sep()  { printf "${BG_HDR}${BOLD}${WHT}  %-54s${RST}\n" "$1"; }  # section header

_find_latest_job_id() {
  [[ -f "$LOG" ]] || return 1
  grep -aoE '/api/export-jobs/[0-9a-f-]+' "$LOG" 2>/dev/null | awk -F/ '{print $NF}' | tail -n 1
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

  if _step_running "transcription"; then
    printf "$(_lbl 'Status')${YEL}${BOLD}▶ transcribing...${RST}\n"
  elif _step_running "diarization"; then
    printf "$(_lbl 'Status')${YEL}${BOLD}▶ diarizing...${RST}\n"
  fi

  if [[ -f "$LOG" ]]; then
    # Last REAL transcription (elapsed > 1s, excludes reuse=True which is ~0s)
    local tx_real
    tx_real="$(grep -aF '[step] transcription | done' "$LOG" | \
      awk -F'elapsed=' 'NF>1 { v=$2+0; if(v>1) print }' | tail -n 1 || true)"
    if [[ -n "$tx_real" ]]; then
      local el ch cps
      el="$(echo "$tx_real"  | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
      ch="$(echo "$tx_real"  | grep -oE 'chars=[0-9]+'     | grep -oE '[0-9.]+' || true)"
      if [[ -n "$el" && -n "$ch" && "${el%.*}" -gt 0 ]]; then
        cps="$(echo "scale=0; $ch / $el" | bc -l 2>/dev/null || echo "?")"
        printf "$(_lbl 'Last run')${GRN}%ss${RST} · %s chars  ${DIM}(~%s chars/s)${RST}\n" "$el" "$ch" "$cps"
      else
        printf "$(_lbl 'Last run')${GRN}%ss${RST}\n" "$el"
      fi
    else
      printf "$(_lbl 'Last run')${DIM}no full Whisper run yet${RST}\n"
    fi

    # Diarization
    local dz; dz="$(grep -aF '[step] diarization | done' "$LOG" | tail -n 1 || true)"
    if [[ -n "$dz" ]]; then
      local de ds
      de="$(echo "$dz" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
      ds="$(echo "$dz" | grep -oE 'spans=[0-9]+'     | grep -oE '[0-9.]+' || true)"
      printf "$(_lbl 'Diarization')${DIM}%ss  %s speakers${RST}\n" "${de:-?}" "${ds:-?}"
    fi
  fi
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
      printf "$(_lbl 'Config')${DIM}%s${RST}  ${DIM}(not loaded)${RST}\n" "${model_cfg:-?}"
    fi
  else
    printf "$(_lbl 'Config')${DIM}%s${RST}\n" "${model_cfg:-?}"
  fi

  if [[ ! -f "$LOG" ]]; then return; fi

  # Currently summarizing?
  if _step_running "summarize_pass1" || _step_running "summarize_pass2"; then
    printf "$(_lbl 'Status')${YEL}${BOLD}▶ summarizing...${RST}\n"
  fi

  # Speed
  local tok_s; tok_s="$(grep -aoE '[0-9.]+ tok/s' "$LOG" | tail -n 1 || true)"
  [[ -n "$tok_s" ]] && printf "$(_lbl 'Speed')${GRN}${BOLD}%s${RST}\n" "$tok_s"

  # Last summarization total
  local sum_done; sum_done="$(grep -aF '[step] summarization | done' "$LOG" | tail -n 1 || true)"
  if [[ -n "$sum_done" ]]; then
    local se; se="$(echo "$sum_done" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
    [[ -n "$se" ]] && printf "$(_lbl 'Last run')${GRN}%ss total${RST}\n" "$se"
  fi

  # Pass breakdown
  local p1 p2 p1_el p2_el
  p1="$(grep -aF '[step] summarize_pass1 | done' "$LOG" | tail -n 1 || true)"
  p2="$(grep -aF '[step] summarize_pass2 | done' "$LOG" | tail -n 1 || true)"
  p1_el="$(echo "$p1" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
  p2_el="$(echo "$p2" | grep -oE 'elapsed=[0-9.]+s' | grep -oE '[0-9.]+' || true)"
  if [[ -n "$p1_el" || -n "$p2_el" ]]; then
    local parts=""
    [[ -n "$p1_el" ]] && parts+="pass1 ${p1_el}s"
    [[ -n "$p1_el" && -n "$p2_el" ]] && parts+="  "
    [[ -n "$p2_el" ]] && parts+="pass2 ${p2_el}s"
    printf "$(_lbl 'LLM passes')${DIM}%s${RST}\n" "$parts"
  fi

  # Context mode (full_transcript / compressed_pack / chunked_extraction)
  local ctx_mode; ctx_mode="$(grep -aF '[step] summarize_pass1 | start' "$LOG" | \
    tail -n 1 | grep -oE 'mode=[a-z_]+' | cut -d= -f2 || true)"
  [[ -n "$ctx_mode" ]] && printf "$(_lbl 'Context')${DIM}%s${RST}\n" "$ctx_mode"
}

# ── EXPORT JOB ────────────────────────────────────────────────────────────────
_job() {
  local job_id; job_id="$(_find_latest_job_id 2>/dev/null || true)"
  if [[ -z "${job_id:-}" ]]; then
    printf "$(_lbl 'Job')${DIM}no export job in log${RST}\n"; return
  fi
  if ! command -v jq &>/dev/null; then
    printf "$(_lbl 'Job')${DIM}jq not installed${RST}\n"; return
  fi

  local raw; raw="$(curl -sS --max-time 1 "${BASE_URL}/api/export-jobs/${job_id}" 2>/dev/null || true)"
  if [[ -z "$raw" ]]; then
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

  local sc
  case "$status" in
    completed) sc="${GRN}${BOLD}✓ completed${RST}" ;;
    failed)    sc="${RED}${BOLD}✗ failed${RST}" ;;
    running)   sc="${YEL}▶ running${RST}" ;;
    queued)    sc="${DIM}queued${RST}" ;;
    *)         sc="${DIM}$status${RST}" ;;
  esac

  printf "$(_lbl 'Job')%.8s…  %b  ${DIM}%s${RST}\n"     "$job_id" "$sc" "$ts"
  printf "$(_lbl 'Stage')${BOLD}%s${RST}  ${DIM}%.45s${RST}\n" "$stage" "$msg"
  printf "$(_lbl 'Transcript')%s\n"                       "$(_bar_prog "$tp")"
  printf "$(_lbl 'Summary')%s\n"                          "$(_bar_prog "$sp")"
  printf "$(_lbl 'Overall')%s\n"                          "$(_bar_prog "$op")"

  if [[ "$status" == "failed" ]]; then
    local err; err="$(printf '%s' "$raw" | jq -r '.error // ""' 2>/dev/null || true)"
    [[ -n "$err" ]] && printf "$(_lbl 'Error')${RED}%.55s${RST}\n" "$err"
  fi
}

# ── PIPELINE ─────────────────────────────────────────────────────────────────
_pipeline() {
  if [[ ! -f "$LOG" ]]; then printf "$(_lbl 'Status')${DIM}no log${RST}\n"; return; fi
  local lines; lines="$(grep -aF '[step]' "$LOG" | tail -n 6 || true)"
  if [[ -z "$lines" ]]; then printf "$(_lbl 'Status')${DIM}no activity${RST}\n"; return; fi

  while IFS= read -r raw; do
    local line="${raw#*\[step\] }"
    local name="${line%% | *}"
    local rest="${line#*| }"; rest="${rest# }"
    local status="${rest%% | *}"
    local meta="${rest#"$status"}"; meta="${meta# | }"

    local clr sym
    case "$status" in
      done)  clr="$GRN"; sym="✓" ;;
      start) clr="$CYN"; sym="→" ;;
      error) clr="$RED"; sym="✗" ;;
      *)     clr="$YEL"; sym="·" ;;
    esac
    printf "  ${clr}${sym}${RST} ${BOLD}%-26s${RST}${clr}%-6s${RST}  ${DIM}%.38s${RST}\n" \
      "$name" "$status" "$meta"
  done <<< "$lines"
}

# ── Draw ─────────────────────────────────────────────────────────────────────
# Use alternate screen buffer so we can redraw from top-left without flicker
# or line-count fragility. Restores original screen on exit.
printf '\033[?1049h\033[?25l'   # enter alt screen, hide cursor
trap 'printf "\033[?1049l\033[?25h"' EXIT INT TERM HUP

_draw() {
  printf '\033[H'   # cursor to top-left (home)

  printf "${BOLD}${WHT}  ⬡  SIDEKICK MONITOR${RST}  ${DIM}%s  (Ctrl+C to stop)${RST}\n\n" \
    "$(date +%H:%M:%S)"

  _sep '  GPU'
  _gpu
  echo

  _sep '  WHISPER  (transcription)'
  _whisper
  echo

  _sep '  OLLAMA  (summarization)'
  _ollama
  echo

  _sep '  EXPORT JOB'
  _job
  echo

  _sep '  PIPELINE  (recent steps)'
  _pipeline

  printf '\033[J'   # clear anything left over below current position
}

while true; do
  _draw
  sleep "$INTERVAL"
done
