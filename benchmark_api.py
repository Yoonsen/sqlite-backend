#!/usr/bin/env python3
import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, str, float]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return resp.status, body, elapsed_ms
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return exc.code, body, elapsed_ms


def _extract_summary(body: str) -> str:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "non-json response"

    if isinstance(parsed, dict):
        for key in ("total", "docs", "count"):
            if key in parsed:
                return f"{key}={parsed[key]}"
        if "rows" in parsed and isinstance(parsed["rows"], list):
            return f"rows={len(parsed['rows'])}"
        return f"keys={','.join(sorted(parsed.keys()))}"
    return type(parsed).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed API benchmark matrix.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="API base URL, for example http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--payloads",
        default="benchmark_payloads.json",
        help="Path to benchmark payloads JSON file.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="How many times each case is repeated.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=40.0,
        help="HTTP timeout per request in seconds.",
    )
    args = parser.parse_args()

    payload_path = Path(args.payloads)
    cases = json.loads(payload_path.read_text(encoding="utf-8"))

    print(f"Base URL: {args.base_url}")
    print(f"Payload file: {payload_path}")
    print(f"Repeats per case: {args.repeats}")
    print("")
    print("case                                 status   ms(avg)   ms(p50)   summary")
    print("--------------------------------------------------------------------------")

    failures = 0
    for case in cases:
        name = case["name"]
        endpoint = case["endpoint"]
        payload = case["payload"]
        url = f"{args.base_url.rstrip('/')}{endpoint}"

        all_times = []
        last_status = 0
        last_summary = ""
        for _ in range(args.repeats):
            status, body, elapsed_ms = _post_json(url, payload, args.timeout)
            all_times.append(elapsed_ms)
            last_status = status
            last_summary = _extract_summary(body)
            if status >= 400:
                failures += 1

        avg_ms = statistics.mean(all_times)
        p50_ms = statistics.median(all_times)
        print(
            f"{name[:36]:36} {last_status:>6} {avg_ms:9.1f} {p50_ms:9.1f}   {last_summary}"
        )

    print("--------------------------------------------------------------------------")
    print(f"Done. Total cases: {len(cases)}")
    if failures:
        print(f"HTTP failures observed: {failures}")
        return 1
    print("No HTTP failures observed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
