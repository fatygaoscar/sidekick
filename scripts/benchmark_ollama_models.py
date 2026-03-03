#!/usr/bin/env python3
"""Benchmark Ollama models against Sidekick transcript workloads.

Uses a real recording transcript from Sidekick and runs extraction-style calls
against one or more models, reporting latency and Ollama timing metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


EXTRACTION_SYSTEM_PROMPT = """You are an expert meeting analyst.
Extract actions, decisions, risks, questions, and follow-ups.
Return valid JSON with keys: actions, decisions, risks, questions, followups."""


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _pick_latest_recording_id(base_url: str, timeout: float) -> str:
    payload = _http_json("GET", f"{base_url}/api/recordings", timeout_seconds=timeout)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("No recordings returned from /api/recordings")
    rec_id = payload[0].get("id")
    if not rec_id:
        raise RuntimeError("Latest recording payload missing id")
    return str(rec_id)


def _build_chunk_text(transcript_rows: list[dict[str, Any]], chunk_seconds: int) -> str:
    if not transcript_rows:
        raise RuntimeError("Recording has no transcript rows")

    start = float(transcript_rows[0].get("start_time", 0.0))
    end = start + float(chunk_seconds)
    lines: list[str] = []

    for row in transcript_rows:
        ts = row.get("timestamp", "[00:00]")
        txt = str(row.get("text", "")).strip()
        st = float(row.get("start_time", start))
        if st > end and lines:
            break
        if txt:
            lines.append(f"{ts} {txt}")

    if not lines:
        raise RuntimeError("Could not build chunk text from transcript rows")
    return "\n".join(lines)


def _run_model_once(
    ollama_url: str,
    model: str,
    chunk_text: str,
    timeout_seconds: float,
    context_length: int,
) -> dict[str, Any]:
    user_prompt = (
        "Extract all actions, decisions, risks, questions, and follow-ups from this transcript segment.\n\n"
        "TRANSCRIPT SEGMENT:\n"
        f"{chunk_text}\n"
        "Return strict JSON."
    )
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"num_ctx": context_length, "think": False},
    }
    t0 = time.perf_counter()
    response = _http_json("POST", f"{ollama_url}/api/chat", payload=payload, timeout_seconds=timeout_seconds)
    elapsed = time.perf_counter() - t0

    content = (
        response.get("message", {}).get("content", "")
        if isinstance(response.get("message"), dict)
        else ""
    )

    eval_count = response.get("eval_count") or 0
    eval_duration_ns = response.get("eval_duration") or 0
    tok_s = eval_count / (eval_duration_ns / 1e9) if eval_duration_ns > 0 else 0

    return {
        "elapsed_seconds": elapsed,
        "response_chars": len(content),
        "tok_s": tok_s,
        "total_duration_ns": response.get("total_duration"),
        "load_duration_ns": response.get("load_duration"),
        "prompt_eval_count": response.get("prompt_eval_count"),
        "eval_count": eval_count,
        "done_reason": response.get("done_reason"),
    }


def _ns_to_s(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1_000_000_000.0
    return None


def _fmt(x: Any) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.2f}"
    return str(x)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Ollama models using Sidekick transcript chunks.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Sidekick base URL")
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        help="Ollama base URL",
    )
    parser.add_argument("--recording-id", help="Recording/session id (defaults to latest)")
    parser.add_argument(
        "--models",
        default="qwen3.5:27b",
        help="Comma-separated model list",
    )
    parser.add_argument("--chunk-seconds", type=int, default=600, help="Transcript seconds to benchmark")
    parser.add_argument("--runs", type=int, default=1, help="Runs per model")
    parser.add_argument("--timeout-seconds", type=float, default=240.0, help="Per-call timeout")
    parser.add_argument(
        "--context-length",
        type=int,
        default=int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "3072")),
        help="num_ctx for benchmark calls",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    ollama_url = args.ollama_url.rstrip("/")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        raise RuntimeError("No models provided")

    recording_id = args.recording_id or _pick_latest_recording_id(base_url, timeout=args.timeout_seconds)
    rec = _http_json("GET", f"{base_url}/api/recordings/{urllib.parse.quote(recording_id)}", timeout_seconds=args.timeout_seconds)
    transcript_rows = rec.get("transcript", [])
    chunk_text = _build_chunk_text(transcript_rows, chunk_seconds=args.chunk_seconds)

    print(f"Benchmark recording_id={recording_id}")
    print(f"Transcript rows={len(transcript_rows)} chunk_seconds={args.chunk_seconds} chunk_chars={len(chunk_text)}")
    print(f"Ollama URL={ollama_url} context={args.context_length} runs={args.runs}")
    print("")

    results: dict[str, list[dict[str, Any]]] = {}
    for model in models:
        model_runs: list[dict[str, Any]] = []
        print(f"Model: {model}")
        for run_idx in range(1, args.runs + 1):
            try:
                result = _run_model_once(
                    ollama_url=ollama_url,
                    model=model,
                    chunk_text=chunk_text,
                    timeout_seconds=args.timeout_seconds,
                    context_length=args.context_length,
                )
                model_runs.append(result)
                print(
                    f"  run {run_idx}: elapsed={result['elapsed_seconds']:.2f}s "
                    f"tok/s={result['tok_s']:.0f} "
                    f"tokens={result['eval_count']} "
                    f"chars={result['response_chars']}"
                )
            except urllib.error.URLError as exc:
                print(f"  run {run_idx}: ERROR url={exc}")
            except TimeoutError:
                print(f"  run {run_idx}: ERROR timeout>{args.timeout_seconds}s")
            except Exception as exc:  # noqa: BLE001
                print(f"  run {run_idx}: ERROR {exc}")
        results[model] = model_runs
        print("")

    print("Summary (successful runs only):")
    print("model | runs | avg_s | min_s | max_s | avg tok/s")
    for model in models:
        rows = results.get(model, [])
        if not rows:
            print(f"{model} | 0 | - | - | - | -")
            continue
        elapsed = [float(r["elapsed_seconds"]) for r in rows]
        tok_rates = [float(r["tok_s"]) for r in rows if r.get("tok_s")]
        avg_tok_s = f"{statistics.mean(tok_rates):.0f}" if tok_rates else "-"
        avg = statistics.mean(elapsed)
        print(
            f"{model} | {len(elapsed)} | {avg:.2f} | "
            f"{min(elapsed):.2f} | {max(elapsed):.2f} | {avg_tok_s}"
        )

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "recording_id": recording_id,
        "chunk_seconds": args.chunk_seconds,
        "chunk_chars": len(chunk_text),
        "ollama_url": ollama_url,
        "context_length": args.context_length,
        "runs_per_model": args.runs,
        "results": results,
    }
    out_name = f"data/model-benchmark-{int(time.time())}.json"
    with open(out_name, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("")
    print(f"Wrote report: {out_name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
