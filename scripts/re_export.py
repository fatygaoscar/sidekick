#!/usr/bin/env python3
"""Re-run the full export pipeline (transcription + diarization + summary) on an existing recording.

Hits the same endpoint as the UI post-recording flow.

Examples:
    # Re-export latest recording with default template
    ./venv/bin/python3 scripts/re_export.py

    # Re-export specific recording
    ./venv/bin/python3 scripts/re_export.py --recording-id abc123

    # With attendees and template
    ./venv/bin/python3 scripts/re_export.py --template one_on_one --attendees "Alice, Bob"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


def _http_get(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url=url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, body: dict[str, Any], timeout: float = 30.0) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_latest_recording(base_url: str, recording_id: str | None) -> dict[str, Any]:
    recordings = _http_get(f"{base_url}/api/recordings", timeout=30.0)
    if not isinstance(recordings, list) or not recordings:
        raise RuntimeError("No recordings returned from Sidekick")
    if recording_id:
        match = next((r for r in recordings if r["id"] == recording_id), None)
        if not match:
            raise RuntimeError(f"Recording {recording_id!r} not found")
        return match
    return recordings[0]


def _poll_job(base_url: str, job_id: str, poll_interval: float = 1.5) -> dict[str, Any]:
    url = f"{base_url}/api/export-jobs/{job_id}"
    last_message = ""
    last_stage = ""

    while True:
        job = _http_get(url, timeout=30.0)
        status = job.get("status", "")
        stage = job.get("stage", "")
        message = job.get("message", "")
        overall = job.get("overall_progress", 0.0)
        t_prog = job.get("transcription_progress", 0.0)
        s_prog = job.get("summarization_progress", 0.0)

        if stage != last_stage or message != last_message:
            bar_len = 30
            filled = int(overall * bar_len)
            bar = "#" * filled + "-" * (bar_len - filled)
            print(
                f"\r[{bar}] {overall * 100:5.1f}%  {stage:<18}  {message[:50]:<50}",
                end="",
                flush=True,
            )
            last_stage = stage
            last_message = message

        if status == "completed":
            print()  # newline after progress bar
            return job
        if status == "failed":
            print()
            raise RuntimeError(job.get("error") or "Export job failed")

        time.sleep(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-run full export pipeline on an existing Sidekick recording.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Sidekick base URL")
    parser.add_argument("--recording-id", help="Recording ID (default: latest)")
    parser.add_argument("--template", default="meeting", help="Summary template key")
    parser.add_argument("--attendees", default="", help="Comma-separated attendee names")
    parser.add_argument("--custom-prompt", default="", help="Custom prompt (for custom template)")
    args = parser.parse_args()

    rec = _fetch_latest_recording(args.base_url, args.recording_id)
    recording_id = rec["id"]
    title = rec.get("title") or recording_id
    duration = int(rec.get("duration") or 0)

    print(f"Recording : {recording_id}")
    print(f"Title     : {title}")
    print(f"Duration  : {duration // 60}m {duration % 60}s")
    print(f"Template  : {args.template}")
    if args.attendees:
        print(f"Attendees : {args.attendees}")
    print()

    body: dict[str, Any] = {"title": title, "template": args.template}
    if args.attendees:
        body["attendees"] = args.attendees
    if args.custom_prompt:
        body["custom_prompt"] = args.custom_prompt

    print("Starting export job...")
    job_create = _http_post(
        f"{args.base_url}/api/recordings/{urllib.parse.quote(recording_id)}/export-obsidian-job",
        body=body,
    )
    job_id = job_create["job_id"]
    print(f"Job ID    : {job_id}\n")

    t0 = time.perf_counter()
    job = _poll_job(args.base_url, job_id)
    elapsed = time.perf_counter() - t0

    result = job.get("result") or {}
    print(f"\nCompleted in {elapsed:.1f}s")
    if result.get("filename"):
        print(f"Saved     : {result['filename']}")
    if result.get("summary_preview"):
        print(f"\n--- Summary preview ---\n{result['summary_preview'][:600]}\n...")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
