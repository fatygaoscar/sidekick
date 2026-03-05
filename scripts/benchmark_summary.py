#!/usr/bin/env python3
"""Comprehensive benchmark for Sidekick summarization pipeline.

Tests the full two-pass cohesive summary pipeline against one or more
model/context configurations, reporting speed and quality metrics.

Must be run with the project virtualenv:
    ./venv/bin/python3 scripts/benchmark_summary.py [options]

Examples:
    # Test current model with default context (reads from env)
    ./venv/bin/python3 scripts/benchmark_summary.py

    # Compare two models across two context lengths
    ./venv/bin/python3 scripts/benchmark_summary.py \\
        --models qwen3.5:4b,qwen3.5:9b \\
        --contexts 16384,32768

    # Test specific recording with working_session template
    ./venv/bin/python3 scripts/benchmark_summary.py \\
        --recording-id abc123 --template working_session
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Allow project imports when run from repo root or scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.summarization.cohesive import generate_cohesive_summary  # noqa: E402
from src.summarization.prompts import get_template_content  # noqa: E402


# Expected core section headers per template (subset used for scoring)
_EXPECTED_HEADERS: dict[str, list[str]] = {
    "meeting": ["## Summary", "## Key Decisions", "## Action Items", "## Discussion Notes"],
    "one_on_one": ["## Summary", "## Action Items"],
    "standup": ["## Updates", "## Action Items"],
    "working_session": ["## Session Focus", "## Work Completed", "## Technical Decisions", "## Action Items"],
    "custom": [],
}

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ARTIFACT_RE = re.compile(r"<\|[^|]+\|>|SPEAKER_\d+")


# ---------------------------------------------------------------------------
# Transcript fetching
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url=url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_transcript(base_url: str, recording_id: str | None, timeout: float) -> tuple[str, str, int]:
    """Return (recording_id, transcript_text, duration_seconds)."""
    if not recording_id:
        recordings = _http_get(f"{base_url}/api/recordings", timeout=timeout)
        if not isinstance(recordings, list) or not recordings:
            raise RuntimeError("No recordings returned from Sidekick")
        recording_id = recordings[0]["id"]
        print(f"Using latest recording: {recording_id}")

    rec = _http_get(
        f"{base_url}/api/recordings/{urllib.parse.quote(recording_id)}", timeout=timeout
    )
    rows = rec.get("transcript", [])
    if not rows:
        raise RuntimeError(f"Recording {recording_id} has no transcript segments")

    lines: list[str] = []
    for row in rows:
        ts = row.get("timestamp", "[00:00]")
        speaker = row.get("speaker", "")
        text = str(row.get("text", "")).strip()
        if text:
            prefix = f"{speaker}: " if speaker else ""
            lines.append(f"{ts} {prefix}{text}")

    duration = int(rec.get("duration", 0) or 0)
    return recording_id, "\n".join(lines), duration


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def _check_quality(summary: str, template: str) -> dict[str, Any]:
    expected = _EXPECTED_HEADERS.get(template, [])
    present = [h for h in expected if h in summary]
    has_action_table = bool(re.search(r"\|\s*Owner\s*\|", summary, re.IGNORECASE))
    has_artifacts = bool(_ARTIFACT_RE.search(summary)) or bool(_THINK_BLOCK_RE.search(summary))
    all_headers = [line.strip() for line in summary.splitlines() if line.startswith("## ")]
    return {
        "expected_headers": f"{len(present)}/{len(expected)}",
        "missing_headers": [h for h in expected if h not in summary],
        "has_action_items_table": has_action_table,
        "has_artifacts": has_artifacts,
        "all_headers": all_headers,
    }


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def _run_config(
    model: str,
    context_length: int,
    ollama_url: str,
    transcript: str,
    template: str,
    template_contract: str,
    timeout: float,
) -> dict[str, Any]:
    """Run the full cohesive summary pipeline for one model/context config."""
    import ollama  # noqa: PLC0415 — imported here to keep top-level clean

    client = ollama.AsyncClient(host=ollama_url)
    call_log: list[dict[str, Any]] = []

    async def llm_call(sys_prompt: str, usr_prompt: str) -> str:
        call_idx = len(call_log) + 1
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": usr_prompt},
                    ],
                    options={"num_ctx": context_length, "think": False, "num_gpu": 99, "temperature": 0.3},
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"call {call_idx} timed out after {timeout}s")

        elapsed = time.perf_counter() - t0
        content = resp["message"]["content"]
        content = _THINK_BLOCK_RE.sub("", content).strip()

        eval_count = resp.get("eval_count") or 0
        eval_ns = resp.get("eval_duration") or 0
        prompt_tokens = resp.get("prompt_eval_count") or 0
        tok_s = eval_count / (eval_ns / 1e9) if eval_ns > 0 else 0.0

        call_log.append({
            "call": call_idx,
            "elapsed_s": round(elapsed, 2),
            "prompt_tokens": prompt_tokens,
            "output_tokens": eval_count,
            "tok_s": round(tok_s, 1),
        })
        print(
            f"    call {call_idx}: {elapsed:.1f}s  {tok_s:.0f} tok/s"
            f"  ({prompt_tokens} in / {eval_count} out)"
        )
        return content

    t0_total = time.perf_counter()
    try:
        summary, context_mode, passes_used, _, _ = await generate_cohesive_summary(
            llm_call=llm_call,
            transcript=transcript,
            template=template,
            template_contract=template_contract,
            context_length=context_length,
        )
    except TimeoutError as exc:
        return {"error": str(exc), "model": model, "context_length": context_length}
    except Exception as exc:
        return {"error": str(exc), "model": model, "context_length": context_length}

    total_elapsed = time.perf_counter() - t0_total
    quality = _check_quality(summary, template)

    return {
        "model": model,
        "context_length": context_length,
        "total_elapsed_s": round(total_elapsed, 2),
        "context_mode": context_mode,
        "passes_used": passes_used,
        "llm_calls": len(call_log),
        "call_log": call_log,
        "output_words": len(summary.split()),
        "quality": quality,
        "summary_preview": summary[:600],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _config_label(model: str, ctx: int) -> str:
    return f"{model}@{ctx}"


def _is_ok(r: dict[str, Any]) -> bool:
    if "error" in r:
        return False
    q = r["quality"]
    parts = q["expected_headers"].split("/")
    headers_ok = len(parts) == 2 and parts[0] == parts[1]
    return r["context_mode"] == "full_transcript" and not q["has_artifacts"] and headers_ok


def _print_summary_table(results: list[dict[str, Any]]) -> None:
    w = 90
    print("\n" + "=" * w)
    print(
        f"{'Config':<30} {'Time':>7}  {'Mode':<18} {'Passes':>6}  "
        f"{'Calls':>5}  {'Words':>6}  {'Hdrs':>6}  {'OK?':>4}"
    )
    print("-" * w)
    for r in results:
        if "error" in r:
            label = _config_label(r["model"], r["context_length"])
            print(f"{label:<30}  ERROR: {r['error'][:50]}")
            continue
        label = _config_label(r["model"], r["context_length"])
        q = r["quality"]
        ok = _is_ok(r)
        print(
            f"{label:<30} {r['total_elapsed_s']:>6.1f}s  "
            f"{r['context_mode']:<18} {r['passes_used']:>6}  "
            f"{r['llm_calls']:>5}  {r['output_words']:>6}  "
            f"{q['expected_headers']:>6}  {'✓' if ok else '✗':>4}"
        )
    print("=" * w)


def _print_previews(results: list[dict[str, Any]]) -> None:
    for r in results:
        if "error" in r or "summary_preview" not in r:
            continue
        label = _config_label(r["model"], r["context_length"])
        print(f"\n--- Preview: {label} ---")
        print(r["summary_preview"])
        print("...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Sidekick summarization pipeline across model/context configs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Sidekick base URL")
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        help="Ollama base URL",
    )
    parser.add_argument("--recording-id", help="Recording ID to test (default: latest)")
    parser.add_argument(
        "--models",
        default=os.environ.get("OLLAMA_MODEL", "qwen3.5:9b"),
        help="Comma-separated model names (default: $OLLAMA_MODEL)",
    )
    parser.add_argument(
        "--contexts",
        default=os.environ.get("OLLAMA_CONTEXT_LENGTH", "32768"),
        help="Comma-separated context lengths (default: $OLLAMA_CONTEXT_LENGTH)",
    )
    parser.add_argument("--template", default="meeting", help="Summary template key")
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-call timeout seconds")
    parser.add_argument("--preview", action="store_true", help="Print summary preview for each config")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    contexts = [int(c.strip()) for c in args.contexts.split(",") if c.strip()]
    configs = [(m, c) for m in models for c in contexts]

    print(f"Fetching transcript from {args.base_url} ...")
    recording_id, transcript, duration_s = _fetch_transcript(
        args.base_url, args.recording_id, timeout=30.0
    )
    template_contract = get_template_content(args.template)

    print(
        f"Recording : {recording_id}\n"
        f"Duration  : {duration_s // 60}m {duration_s % 60}s\n"
        f"Transcript: {len(transcript):,} chars\n"
        f"Template  : {args.template}\n"
        f"Configs   : {[f'{m}@{c}' for m, c in configs]}\n"
    )

    results: list[dict[str, Any]] = []
    for model, ctx in configs:
        print(f"--- {model}  ctx={ctx} ---")
        result = await _run_config(
            model=model,
            context_length=ctx,
            ollama_url=args.ollama_url,
            transcript=transcript,
            template=args.template,
            template_contract=template_contract,
            timeout=args.timeout,
        )
        results.append(result)

        if "error" not in result:
            q = result["quality"]
            print(
                f"    total: {result['total_elapsed_s']:.1f}s  "
                f"mode: {result['context_mode']}  "
                f"passes: {result['passes_used']}  "
                f"words: {result['output_words']}  "
                f"headers: {q['expected_headers']}"
            )
            if q["missing_headers"]:
                print(f"    missing sections: {q['missing_headers']}")
            if q["has_artifacts"]:
                print("    WARNING: artifacts detected in output")
        else:
            print(f"    ERROR: {result['error']}")
        print()

    _print_summary_table(results)

    if args.preview:
        _print_previews(results)

    # Save JSON report (strip preview from file to keep size down)
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "recording_id": recording_id,
        "duration_seconds": duration_s,
        "transcript_chars": len(transcript),
        "template": args.template,
        "ollama_url": args.ollama_url,
        "results": [{k: v for k, v in r.items() if k != "summary_preview"} for r in results],
    }
    os.makedirs("data", exist_ok=True)
    out_path = f"data/benchmark-summary-{int(time.time())}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
