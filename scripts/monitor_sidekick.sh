#!/usr/bin/env bash
# Sidekick live monitor — Whisper · Ollama · GPU · Export Job · Workflow
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

_fetch_job_raw() {
  local job_id="$1"
  [[ -n "$job_id" ]] || return 1
  curl -sS --max-time 1 "${BASE_URL}/api/export-jobs/${job_id}" 2>/dev/null || true
}

_job_field() {
  local raw="$1" expr="$2"
  if [[ -z "$raw" ]] || ! command -v jq &>/dev/null; then
    return 0
  fi
  printf '%s' "$raw" | jq -r "$expr // empty" 2>/dev/null || true
}

_job_log_last_fixed() {
  local job_start="$1" pattern="$2"
  [[ -f "$LOG" ]] || return 0
  tail -n +"$job_start" "$LOG" | grep -aF "$pattern" | tail -n 1 || true
}

_job_log_last_regex() {
  local job_start="$1" pattern="$2"
  [[ -f "$LOG" ]] || return 0
  tail -n +"$job_start" "$LOG" | grep -aE "$pattern" | tail -n 1 || true
}

_job_log_has_fixed() {
  local job_start="$1" pattern="$2"
  [[ -f "$LOG" ]] || return 1
  tail -n +"$job_start" "$LOG" | grep -aqF "$pattern"
}

_job_step_running() {
  local job_start="$1" name="$2"
  [[ -f "$LOG" ]] || return 1
  local s d
  s="$(tail -n +"$job_start" "$LOG" | grep -anF "[step] $name | start" | tail -n 1 | cut -d: -f1)"
  d="$(tail -n +"$job_start" "$LOG" | grep -anF "[step] $name | done" | tail -n 1 | cut -d: -f1)"
  [[ -n "$s" && ( -z "$d" || "$s" -gt "$d" ) ]]
}

_extract_field() {
  local line="$1" key="$2"
  sed -nE "s/.*${key}=([^|]+).*/\\1/p" <<< "$line" | sed 's/[[:space:]]*$//' | head -n 1
}

_compact_pairs() {
  local max_items="$1"
  shift || true
  local items=("$@")
  local out="" shown=0 total="${#items[@]}"
  local item
  for item in "${items[@]}"; do
    [[ -n "$item" ]] || continue
    if [[ $shown -ge $max_items ]]; then
      break
    fi
    [[ -n "$out" ]] && out+=", "
    out+="$item"
    shown=$((shown + 1))
  done
  if [[ $total -gt $shown ]]; then
    out+=" +$((total - shown)) more"
  fi
  printf '%s' "$out"
}

_speaker_map_preview() {
  local line="$1"
  [[ -n "$line" ]] || return 0
  local -a raw_pairs pairs
  mapfile -t raw_pairs < <(grep -oE "SPEAKER_[0-9]+': '[^']+'" <<< "$line" || true)
  [[ ${#raw_pairs[@]} -gt 0 ]] || return 0
  local pair
  for pair in "${raw_pairs[@]}"; do
    pairs+=("$(sed -E "s/': '/->/; s/'//g" <<< "$pair")")
  done
  _compact_pairs 3 "${pairs[@]}"
}

_workflow_stage() {
  local name="$1" color="$2" symbol="$3" status="$4" detail="$5"
  printf "  %-14s %b%s %-12s%b" "$name" "$color" "$symbol " "$status" "$RST"
  if [[ -n "$detail" ]]; then
    printf " ${DIM}%s${RST}" "$detail"
  fi
  printf "\n"
}

_workflow_note() {
  local detail="$1"
  [[ -n "$detail" ]] || return 0
  printf "  %-14s ${DIM}%s${RST}\n" "" "$detail"
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

  local raw; raw="$(_fetch_job_raw "$job_id")"
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
}

# ── WORKFLOW ─────────────────────────────────────────────────────────────────
_workflow() {
  local job_start="${1:-1}"
  if [[ ! -f "$LOG" ]]; then
    printf "$(_lbl 'Status')${DIM}no log${RST}\n"
    return
  fi

  local job_id="" job_raw="" job_status="" job_stage="" sum_progress=""
  job_id="$(_find_latest_job_id 2>/dev/null || true)"
  if [[ -n "$job_id" ]]; then
    job_raw="$(_fetch_job_raw "$job_id")"
  fi
  job_status="$(_job_field "$job_raw" '.status')"
  job_stage="$(_job_field "$job_raw" '.stage')"
  sum_progress="$(_job_field "$job_raw" '((.summarization_progress // 0) * 100 | floor | tostring)')"

  local tx_done tx_error tx_running tx_elapsed tx_chars tx_note tx_status tx_detail tx_color tx_symbol
  tx_done="$(_job_log_last_fixed "$job_start" '[step] transcription | done')"
  tx_error="$(_job_log_last_fixed "$job_start" '[step] transcription | error')"
  tx_running=0
  _job_step_running "$job_start" "transcription" && tx_running=1
  tx_elapsed="$(_extract_field "$tx_done" 'elapsed')"
  tx_chars="$(_extract_field "$tx_done" 'chars')"
  tx_note=""
  if _job_log_has_fixed "$job_start" '[step] transcription | unloaded model to free VRAM'; then
    tx_note="Whisper unloaded for summarization"
  fi
  if [[ -n "$tx_error" ]]; then
    tx_status="error"; tx_color="$RED"; tx_symbol="✗"; tx_detail="${tx_elapsed:+${tx_elapsed}s}"
  elif [[ $tx_running -eq 1 ]]; then
    tx_status="running"; tx_color="$YEL"; tx_symbol="▶"; tx_detail="${tx_chars:+chars=${tx_chars}}"
  elif [[ -n "$tx_done" ]]; then
    if [[ -n "$tx_elapsed" && "${tx_elapsed%.*}" -le 1 ]]; then
      tx_status="reused"; tx_color="$CYN"; tx_symbol="↺"
      tx_detail="cached transcript${tx_chars:+ | chars=${tx_chars}}"
    else
      tx_status="done"; tx_color="$GRN"; tx_symbol="✓"
      tx_detail="${tx_elapsed:+${tx_elapsed}s}${tx_chars:+ | chars=${tx_chars}}"
    fi
  else
    tx_status="waiting"; tx_color="$CYN"; tx_symbol="·"; tx_detail=""
  fi
  _workflow_stage "Transcription" "$tx_color" "$tx_symbol" "$tx_status" "$tx_detail"
  _workflow_note "$tx_note"

  local dz_done dz_error dz_warn dz_running dz_elapsed dz_spans dz_limit dz_status dz_detail dz_color dz_symbol
  dz_done="$(_job_log_last_fixed "$job_start" '[step] diarization | done')"
  dz_error="$(_job_log_last_fixed "$job_start" '[step] diarization | error')"
  dz_warn="$(_job_log_last_regex "$job_start" 'Diarization failed')"
  dz_running=0
  _job_step_running "$job_start" "diarization" && dz_running=1
  dz_elapsed="$(_extract_field "$dz_done" 'elapsed')"
  dz_spans="$(_extract_field "$dz_done" 'spans')"
  dz_limit="$(_extract_field "$(_job_log_last_fixed "$job_start" '[step] diarization | start')" 'limit')"
  if [[ -n "$dz_error" || -n "$dz_warn" ]]; then
    dz_status="error"; dz_color="$RED"; dz_symbol="✗"; dz_detail="${dz_elapsed:+${dz_elapsed}s}"
  elif [[ $dz_running -eq 1 ]]; then
    dz_status="running"; dz_color="$YEL"; dz_symbol="▶"; dz_detail="${dz_limit:+limit=${dz_limit}}"
  elif [[ -n "$dz_done" ]]; then
    dz_status="done"; dz_color="$GRN"; dz_symbol="✓"
    dz_detail="${dz_elapsed:+${dz_elapsed}s}${dz_spans:+ | spans=${dz_spans}}"
  elif [[ -n "$tx_done" || "$job_stage" == "summarizing" || "$job_status" == "completed" || "$job_status" == "failed" ]]; then
    dz_status="not observed"; dz_color="$CYN"; dz_symbol="·"; dz_detail=""
  else
    dz_status="waiting"; dz_color="$CYN"; dz_symbol="·"; dz_detail=""
  fi
  _workflow_stage "Diarization" "$dz_color" "$dz_symbol" "$dz_status" "$dz_detail"

  local sp_start sp_done sp_error sp_skipped sp_skip_reason sp_resolved_line sp_map_line sp_attendees sp_skip_attendees sp_resolved sp_running
  local sp_status sp_detail sp_color sp_symbol sp_preview sp_nulled
  sp_start="$(_job_log_last_fixed "$job_start" '[step] speaker_prepass | start')"
  sp_done="$(_job_log_last_fixed "$job_start" '[step] speaker_prepass | done')"
  sp_error="$(_job_log_last_regex "$job_start" '\\[step\\] speaker_prepass \\| error|speaker_prepass: exception=|speaker_prepass: no JSON found')"
  sp_skipped="$(_job_log_last_fixed "$job_start" '[step] speaker_prepass | skipped')"
  sp_skip_reason="$(_extract_field "$sp_skipped" 'reason')"
  sp_resolved_line="$(_job_log_last_fixed "$job_start" 'speaker_prepass: resolved=')"
  sp_map_line="$(_job_log_last_fixed "$job_start" 'cohesive: speaker_map=')"
  sp_attendees="$(_extract_field "$sp_start" 'attendees')"
  sp_skip_attendees="$(_extract_field "$sp_skipped" 'attendees')"
  sp_resolved="$(_extract_field "$sp_done" 'resolved')"
  sp_nulled="$(sed -nE 's/.*nulled=\[([^]]*)\].*/\1/p' <<< "$sp_resolved_line" | sed 's/[[:space:]]*$//' | head -n 1)"
  sp_preview="$(_speaker_map_preview "$sp_map_line")"
  sp_running=0
  _job_step_running "$job_start" "speaker_prepass" && sp_running=1
  if [[ -n "$sp_error" ]]; then
    sp_status="error"; sp_color="$RED"; sp_symbol="✗"
    sp_detail="${sp_attendees:+attendees=${sp_attendees}}"
  elif [[ $sp_running -eq 1 ]]; then
    sp_status="running"; sp_color="$YEL"; sp_symbol="▶"
    sp_detail="${sp_attendees:+attendees=${sp_attendees}}"
  elif [[ -n "$sp_done" ]]; then
    if [[ -n "$sp_resolved" && "$sp_resolved" != "0" ]]; then
      sp_status="resolved"; sp_color="$GRN"; sp_symbol="✓"
    else
      sp_status="no matches"; sp_color="$CYN"; sp_symbol="·"
    fi
    sp_detail="${sp_attendees:+attendees=${sp_attendees}}${sp_resolved:+ | resolved=${sp_resolved}}"
  elif [[ "$sp_skip_reason" == "no_attendees" ]]; then
    sp_status="no attendees"; sp_color="$CYN"; sp_symbol="·"; sp_detail=""
  elif [[ "$sp_skip_reason" == "no_speaker_labels" ]]; then
    sp_status="no speaker labels"; sp_color="$CYN"; sp_symbol="·"
    sp_detail="${sp_skip_attendees:+attendees=${sp_skip_attendees}}"
  elif [[ -n "$(_job_log_last_fixed "$job_start" '[step] summarization | start')" || -n "$(_job_log_last_fixed "$job_start" '[step] summarize_pass1 | start')" || "$job_stage" == "summarizing" || "$job_status" == "completed" || "$job_status" == "failed" ]]; then
    sp_status="not triggered"; sp_color="$CYN"; sp_symbol="·"; sp_detail=""
  else
    sp_status="waiting"; sp_color="$CYN"; sp_symbol="·"; sp_detail=""
  fi
  _workflow_stage "Speaker Names" "$sp_color" "$sp_symbol" "$sp_status" "$sp_detail"
  if [[ -n "$sp_preview" ]]; then
    _workflow_note "$sp_preview"
  elif [[ -n "$sp_nulled" && "$sp_status" != "waiting" ]]; then
    _workflow_note "unmatched labels: ${sp_nulled}"
  fi

  local sum_done sum_error sum_running p1 p2 p1_el p2_el ctx_mode tok_s sum_status sum_detail sum_color sum_symbol
  sum_done="$(_job_log_last_fixed "$job_start" '[step] summarization | done')"
  sum_error="$(_job_log_last_fixed "$job_start" '[step] summarization | error')"
  sum_running=0
  if _job_step_running "$job_start" "summarize_pass1" || _job_step_running "$job_start" "summarize_pass2"; then
    sum_running=1
  fi
  p1="$(_job_log_last_fixed "$job_start" '[step] summarize_pass1 | done')"
  p2="$(_job_log_last_fixed "$job_start" '[step] summarize_pass2 | done')"
  p1_el="$(_extract_field "$p1" 'elapsed')"
  p2_el="$(_extract_field "$p2" 'elapsed')"
  ctx_mode="$(_extract_field "$(_job_log_last_fixed "$job_start" '[step] summarize_pass1 | start')" 'mode')"
  tok_s="$(_job_log_last_regex "$job_start" '[0-9.]+ tok/s' | grep -aoE '[0-9.]+ tok/s' | tail -n 1 || true)"
  if [[ -n "$sum_error" || ( "$job_status" == "failed" && "$job_stage" == "failed" ) ]]; then
    sum_status="error"; sum_color="$RED"; sum_symbol="✗"
    sum_detail="${sum_progress:+${sum_progress}%}${ctx_mode:+ | ${ctx_mode}}"
  elif [[ $sum_running -eq 1 || "$job_stage" == "summarizing" || ( "$job_status" == "running" && -n "$sum_progress" && "$sum_progress" != "0" ) ]]; then
    sum_status="running"; sum_color="$YEL"; sum_symbol="▶"
    sum_detail="${sum_progress:+${sum_progress}%}${ctx_mode:+ | ${ctx_mode}}"
  elif [[ -n "$sum_done" || "$job_status" == "completed" ]]; then
    sum_status="done"; sum_color="$GRN"; sum_symbol="✓"
    sum_detail="${sum_progress:+${sum_progress}%}${ctx_mode:+ | ${ctx_mode}}"
  else
    sum_status="waiting"; sum_color="$CYN"; sum_symbol="·"; sum_detail=""
  fi
  if [[ -n "$p1_el" || -n "$p2_el" ]]; then
    local pass_detail=""
    [[ -n "$p1_el" ]] && pass_detail+="pass1 ${p1_el}s"
    [[ -n "$p1_el" && -n "$p2_el" ]] && pass_detail+=" | "
    [[ -n "$p2_el" ]] && pass_detail+="pass2 ${p2_el}s"
    if [[ -n "$sum_detail" ]]; then
      sum_detail+=" | ${pass_detail}"
    else
      sum_detail="$pass_detail"
    fi
  fi
  _workflow_stage "Summary" "$sum_color" "$sum_symbol" "$sum_status" "$sum_detail"
  if [[ -n "$tok_s" ]]; then
    _workflow_note "speed ${tok_s}"
  fi
}

# ── Poll for keypress (non-blocking) ─────────────────────────────────────────
_poll_input() {
  local k=""
  # Drain ALL pending input; discard mouse escape sequences
  while IFS= read -r -s -N1 -t0 k 2>/dev/null; do
    if [[ "${k:-}" == $'\033' ]]; then
      IFS= read -r -s -N64 -t0.05 _ 2>/dev/null || true
    fi
  done
}

# ── Cleanup ───────────────────────────────────────────────────────────────────
_stty_orig=$(stty -g 2>/dev/null || true)
_cleanup() {
  # Disable mouse tracking + restore screen — must go to /dev/tty directly
  if [[ -t 1 && -w /dev/tty ]]; then
    printf '\033[?1000l\033[?1006l\033[?1049l\033[?25h' > /dev/tty 2>/dev/null || true
  fi
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
    _sep 'WORKFLOW'; _workflow "$job_start"
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
