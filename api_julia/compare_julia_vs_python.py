#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


def run_julia_probe(
    julia_script: str,
    payload: Dict[str, Any],
    postings_config: str,
    timeout_s: int,
) -> Dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(payload, tf, ensure_ascii=False)
        tf.flush()
        payload_path = tf.name

    env = os.environ.copy()
    env["POSTINGS_CONFIG"] = postings_config
    cmd = ["julia", julia_script, payload_path]
    try:
        t0 = time.perf_counter()
        cp = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=True,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        out = json.loads(cp.stdout.strip())
        out["_wall_ms"] = elapsed_ms
        return out
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass


def run_python_endpoint(
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    repeats: int,
    warmup: int,
    timeout_s: int,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    times_ms = []
    last_json: Optional[Dict[str, Any]] = None
    status_codes = []
    for i in range(warmup + repeats):
        t0 = time.perf_counter()
        r = requests.post(url, json=payload, timeout=timeout_s)
        dt = (time.perf_counter() - t0) * 1000.0
        status_codes.append(r.status_code)
        r.raise_for_status()
        last_json = r.json()
        if i >= warmup:
            times_ms.append(dt)
    return {
        "url": url,
        "repeats": repeats,
        "warmup": warmup,
        "avg_ms": statistics.mean(times_ms) if times_ms else 0.0,
        "median_ms": statistics.median(times_ms) if times_ms else 0.0,
        "min_ms": min(times_ms) if times_ms else 0.0,
        "max_ms": max(times_ms) if times_ms else 0.0,
        "status_codes": status_codes,
        "last_response": last_json or {},
    }


def summarize_rows(resp: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "total" in resp:
        out["total"] = resp.get("total")
    if "docs" in resp:
        out["docs"] = resp.get("docs")
    rows = resp.get("rows")
    if isinstance(rows, list):
        out["rows_len"] = len(rows)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare Julia probe vs Python endpoint using same payload."
    )
    parser.add_argument("--payload", required=True, help="Path to payload JSON")
    parser.add_argument(
        "--postings-config",
        default=os.environ.get("POSTINGS_CONFIG", ""),
        help="Path to config JSON (for Julia probe)",
    )
    parser.add_argument(
        "--julia-script",
        default="api_julia/sqlite_blob_julia_probe.jl",
        help="Path to Julia probe script",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Measurement repeats (after warmup)"
    )
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout seconds")
    parser.add_argument(
        "--python-base-url",
        default="",
        help="Optional Python API base URL, e.g. http://127.0.0.1:8032",
    )
    parser.add_argument(
        "--endpoint",
        default="/near_fragments",
        help="Python endpoint path to compare",
    )
    args = parser.parse_args()

    payload_path = Path(args.payload)
    if not payload_path.exists():
        raise SystemExit(f"Payload not found: {payload_path}")
    if not args.postings_config:
        raise SystemExit("--postings-config is required (or set POSTINGS_CONFIG)")

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["repeats"] = args.repeats

    julia = run_julia_probe(
        julia_script=args.julia_script,
        payload=payload,
        postings_config=args.postings_config,
        timeout_s=args.timeout,
    )

    print("Julia:")
    print(
        json.dumps(
            {
                "avg_total_ms": julia.get("avg_total_ms"),
                "median_total_ms": julia.get("median_total_ms"),
                "wall_ms": julia.get("_wall_ms"),
                "last_run_summary": summarize_rows(julia.get("last_run", {})),
                "last_run_timings_ms": julia.get("last_run", {}).get("timings_ms", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if args.python_base_url:
        py_payload = dict(payload)
        py_payload.pop("repeats", None)
        py = run_python_endpoint(
            base_url=args.python_base_url,
            endpoint=args.endpoint,
            payload=py_payload,
            repeats=args.repeats,
            warmup=args.warmup,
            timeout_s=args.timeout,
        )
        print("\nPython:")
        print(
            json.dumps(
                {
                    "url": py["url"],
                    "avg_ms": py["avg_ms"],
                    "median_ms": py["median_ms"],
                    "min_ms": py["min_ms"],
                    "max_ms": py["max_ms"],
                    "last_response_summary": summarize_rows(py["last_response"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
