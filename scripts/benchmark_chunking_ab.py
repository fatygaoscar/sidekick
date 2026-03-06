#!/usr/bin/env python3
"""A/B benchmark full-context vs chunked summarization strategies.

Runs three strategies against the same transcript:
1. full_context_single_pass
2. chunked_extraction_rollup
3. chunked_extraction_overlap_rollup

Each run records exact model settings, token estimates, chunk metadata,
and parse/schema success for the chunked extraction stage.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ollama  # type: ignore

from scripts.benchmark_utils import estimate_tokens, ollama_options, ollama_run_config
from src.summarization.cohesive import (
    CHUNK_PROMPT_VERSION,
    CHUNK_SCHEMA_VERSION,
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP_RATIO,
    _build_pass1_prompt,
    _build_system_prompt,
    generate_cohesive_summary,
)
from src.summarization.prompts import get_template_content


def _http_get(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url=url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_transcript(base_url: str, recording_id: str | None, timeout: float) -> tuple[str, str, list[dict[str, Any]]]:
    if not recording_id:
        recordings = _http_get(f"{base_url}/api/recordings", timeout=timeout)
        if not isinstance(recordings, list) or not recordings:
            raise RuntimeError("No recordings returned from Sidekick")
        recording_id = recordings[0]["id"]

    rec = _http_get(f"{base_url}/api/recordings/{urllib.parse.quote(recording_id)}", timeout=timeout)
    rows = rec.get("transcript", [])
    if not rows:
        raise RuntimeError(f"Recording {recording_id} has no transcript rows")

    lines: list[str] = []
    for row in rows:
        ts = row.get("timestamp", "[00:00]")
        speaker = row.get("speaker", "")
        text = str(row.get("text", "")).strip()
        if text:
            prefix = f"{speaker}: " if speaker else ""
            lines.append(f"{ts} {prefix}{text}")

    return str(recording_id), "\n".join(lines), rows


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _extract_action_rows(summary: str) -> list[str]:
    lines = [line.strip() for line in summary.splitlines()]
    return [line for line in lines if line.startswith("|") and not re.search(r"\|\s*Owner\s*\|", line, re.I)]


def _extract_decision_lines(summary: str) -> list[str]:
    return [line.strip() for line in summary.splitlines() if line.strip().startswith("-")]


def _score_against_gold(summary: str, gold: dict[str, Any]) -> dict[str, Any]:
    """Very simple exact/substring rubric against a saved gold set."""
    gold_actions = [_normalize_text(x) for x in gold.get("action_items", [])]
    gold_decisions = [_normalize_text(x) for x in gold.get("decisions", [])]
    gold_owners = {k: _normalize_text(v) for k, v in (gold.get("owners", {}) or {}).items()}

    pred_actions = [_normalize_text(x) for x in _extract_action_rows(summary)]
    pred_decisions = [_normalize_text(x) for x in _extract_decision_lines(summary)]

    def recall(pred: list[str], gold_items: list[str]) -> float | None:
        if not gold_items:
            return None
        hits = sum(1 for g in gold_items if any(g in p or p in g for p in pred))
        return round(hits / len(gold_items), 3)

    def precision(pred: list[str], gold_items: list[str]) -> float | None:
        if not pred:
            return None
        hits = sum(1 for p in pred if any(g in p or p in g for g in gold_items))
        return round(hits / len(pred), 3)

    owner_hits = 0
    owner_total = 0
    summary_norm = _normalize_text(summary)
    for entity, expected in gold_owners.items():
        owner_total += 1
        if entity.lower() in summary_norm and expected in summary_norm:
            owner_hits += 1

    return {
        "action_items_recall": recall(pred_actions, gold_actions),
        "action_items_precision": precision(pred_actions, gold_actions),
        "decisions_recall": recall(pred_decisions, gold_decisions),
        "owner_attribution_accuracy": round(owner_hits / owner_total, 3) if owner_total else None,
        "omission_rate": None,
        "hallucination_rate": None,
        "readability": None,
    }


def _manual_score_template() -> dict[str, Any]:
    return {
        "action_items_recall": None,
        "action_items_precision": None,
        "decisions_recall": None,
        "owner_attribution_accuracy": None,
        "omission_rate": None,
        "hallucination_rate": None,
        "readability": None,
        "notes": "",
    }


async def _run_strategy(
    client: ollama.AsyncClient,
    transcript: str,
    template: str,
    model: str,
    num_ctx: int,
    strategy: str,
    timeout_s: float,
    chunk_chars: int,
    overlap_chars: int,
) -> dict[str, Any]:
    config = ollama_run_config(model, num_ctx=num_ctx)
    call_log: list[dict[str, Any]] = []

    async def llm_call(system_prompt: str, user_prompt: str) -> str:
        t0 = time.perf_counter()
        response = await asyncio.wait_for(
            client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                options=ollama_options(model, num_ctx=num_ctx),
            ),
            timeout=timeout_s,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        call_log.append({
            "elapsed_ms": elapsed_ms,
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "estimated_input_tokens": estimate_tokens(system_prompt) + estimate_tokens(user_prompt),
        })
        return response["message"]["content"]

    template_contract = get_template_content(template)
    debug_info: dict[str, Any] = {}
    t0 = time.perf_counter()

    if strategy == "full_context_single_pass":
        system_prompt = _build_system_prompt(template)
        user_prompt = _build_pass1_prompt(
            template=template,
            template_contract=template_contract,
            context_text=transcript,
            context_mode="full_transcript",
            custom_instructions=None,
            include_structured_tables=False,
        )
        debug_info["pass1_prompt_estimated_tokens"] = (
            estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
        )
        summary = await llm_call(system_prompt, user_prompt)
        context_mode = "full_transcript"
        passes_used = 1
        prompt_version = "cohesive_pass1_v1"
        output_schema_version = "summary_markdown_v1"
        output_parse_success = True
        output_schema_success = True
    else:
        force_mode = "chunked_extraction"
        summary, context_mode, passes_used, _, _ = await generate_cohesive_summary(
            llm_call=llm_call,
            transcript=transcript,
            template=template,
            template_contract=template_contract,
            context_length=num_ctx,
            force_context_mode=force_mode,
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
            debug_info=debug_info,
        )
        prompt_version = f"{CHUNK_PROMPT_VERSION}+cohesive_pass1_v1"
        output_schema_version = CHUNK_SCHEMA_VERSION
        diagnostics = debug_info.get("chunk_diagnostics", [])
        output_parse_success = all(d.get("parse_status") == "ok" for d in diagnostics)
        output_schema_success = all(d.get("schema_status") == "ok" for d in diagnostics)

    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "strategy": strategy,
        "summary": summary,
        "context_mode": context_mode,
        "passes_used": passes_used,
        "latency_ms": latency_ms,
        "call_log": call_log,
        "run_config": config,
        "prompt_version": prompt_version,
        "output_schema_version": output_schema_version,
        "output_parse_success": output_parse_success,
        "output_schema_success": output_schema_success,
        "debug": debug_info,
    }


def _table_row(run: dict[str, Any]) -> str:
    return (
        f"{run['chunk_strategy']:<34} "
        f"{run['latency_ms']:>9.1f} "
        f"{run['num_ctx']:>7} "
        f"{run.get('chunk_size', 0):>9} "
        f"{run.get('overlap_size', 0):>8} "
        f"{str(run['output_parse_success']):>7} "
        f"{str(run['output_schema_success']):>8}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark full-context vs chunked summarization strategies.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Sidekick base URL")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    parser.add_argument("--recording-id", help="Recording ID (default: latest)")
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen3:8b"))
    parser.add_argument("--num-ctx", type=int, default=int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "32768")))
    parser.add_argument("--template", default="meeting")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--overlap-size", type=int, help="Overlap chars for overlap strategy")
    parser.add_argument("--gold-path", help="Optional gold-standard JSON for auto scoring")
    args = parser.parse_args()

    overlap_size = args.overlap_size or max(1, int(args.chunk_size * DEFAULT_CHUNK_OVERLAP_RATIO))
    transcript_id, transcript, transcript_rows = _fetch_transcript(args.base_url, args.recording_id, timeout=30.0)
    gold = None
    if args.gold_path:
        with open(args.gold_path, "r", encoding="utf-8") as fh:
            gold = json.load(fh)

    client = ollama.AsyncClient(host=args.ollama_url)
    strategies = [
        ("full_context_single_pass", 0),
        ("chunked_extraction_rollup", 0),
        ("chunked_extraction_overlap_rollup", overlap_size),
    ]

    runs: list[dict[str, Any]] = []
    for strategy, overlap in strategies:
        run = await _run_strategy(
            client=client,
            transcript=transcript,
            template=args.template,
            model=args.model,
            num_ctx=args.num_ctx,
            strategy=strategy,
            timeout_s=args.timeout,
            chunk_chars=args.chunk_size,
            overlap_chars=overlap,
        )
        score = _score_against_gold(run["summary"], gold) if gold else _manual_score_template()
        debug = run.get("debug", {})
        cfg = run["run_config"]
        benchmark_record = {
            "transcript_id": transcript_id,
            "transcript_length_chars": len(transcript),
            "estimated_input_tokens": estimate_tokens(transcript),
            "model_name": cfg["model_name"],
            "model_tag": cfg["model_tag"],
            "runtime": cfg["runtime"],
            "num_ctx": cfg["num_ctx"],
            "temperature": cfg["temperature"],
            "top_p": cfg["top_p"],
            "top_k": cfg["top_k"],
            "repeat_penalty": cfg["repeat_penalty"],
            "seed": cfg["seed"],
            "chunk_strategy": strategy,
            "chunk_size": debug.get("chunk_size", 0),
            "overlap_size": debug.get("overlap_size", 0),
            "prompt_version": run["prompt_version"],
            "output_schema_version": run["output_schema_version"],
            "latency_ms": run["latency_ms"],
            "output_parse_success": run["output_parse_success"],
            "output_schema_success": run["output_schema_success"],
            "full_prompt_estimated_tokens": debug.get("pass1_prompt_estimated_tokens"),
            "chunk_prompt_estimated_tokens": [d.get("estimated_prompt_tokens") for d in debug.get("chunk_diagnostics", [])],
            "merge_prompt_estimated_tokens": debug.get("pass1_prompt_estimated_tokens"),
            "chunk_metadata": debug.get("chunks", []),
            "call_log": run["call_log"],
            "score": score,
            "summary_preview": run["summary"][:800],
        }
        runs.append(benchmark_record)

        print(
            f"model_name={cfg['model_name']} "
            f"model_tag={cfg['model_tag'] or '-'} "
            f"runtime={cfg['runtime']} "
            f"num_ctx={cfg['num_ctx']} "
            f"temperature={cfg['temperature']} "
            f"top_p={cfg['top_p']} "
            f"top_k={cfg['top_k']} "
            f"repeat_penalty={cfg['repeat_penalty']} "
            f"seed={cfg['seed'] if cfg['seed'] is not None else 'none'}"
        )
        print(
            f"strategy={strategy} latency_ms={run['latency_ms']} "
            f"chunk_size={benchmark_record['chunk_size']} overlap_size={benchmark_record['overlap_size']} "
            f"parse_success={benchmark_record['output_parse_success']} "
            f"schema_success={benchmark_record['output_schema_success']}"
        )
        print("")

    print(
        f"{'Strategy':<34} {'Latency':>9} {'num_ctx':>7} {'chunk_sz':>9} "
        f"{'overlap':>8} {'parse':>7} {'schema':>8}"
    )
    print("-" * 92)
    for run in runs:
        print(_table_row(run))

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "transcript_id": transcript_id,
        "transcript_length_chars": len(transcript),
        "transcript_turns": len(transcript_rows),
        "estimated_input_tokens": estimate_tokens(transcript),
        "gold_path": args.gold_path,
        "runs": runs,
        "manual_scoring_rubric": {
            "action_items_recall": "0-1 fraction of gold action items present",
            "action_items_precision": "0-1 fraction of predicted action items supported by transcript/gold",
            "decisions_recall": "0-1 fraction of gold decisions present",
            "owner_attribution_accuracy": "0-1 fraction of owners correctly attributed",
            "omission_rate": "0-1 fraction of important items omitted",
            "hallucination_rate": "0-1 fraction of unsupported claims",
            "readability": "1-5 subjective clarity score",
        },
    }
    os.makedirs("data", exist_ok=True)
    out_path = f"data/benchmark-chunking-ab-{int(time.time())}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nReport saved: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
