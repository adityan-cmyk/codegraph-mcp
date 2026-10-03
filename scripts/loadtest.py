#!/usr/bin/env python3
"""Load-test the MCP server with realistic agent traffic.

Usage: python3 scripts/loadtest.py [concurrency] [requests_per_worker]
Reports per-tool latency percentiles (p50/p95/p99) and error rates.
Per-tool latency targets live in ROADMAP.md — this measures against them.
"""
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

MCP = os.environ.get("MCP_URL", "http://localhost:8002/mcp")
TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
if not TOKEN:
    for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
        if line.startswith("MCP_AUTH_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()

TOOLS = [
    ("search_symbols", {"query": "payment", "limit": 10}),
    ("get_blast_radius", {"symbol_id": "crates::schema-verifier::comparison::indexes::compare_indexes"}),
    ("get_graph_stats", {}),
    ("find_hotspots", {"days": 30, "limit": 5}),
    ("semantic_search", {"query": "chargeback dispute flow", "limit": 5}),
]


def call(tool: str, args: dict) -> tuple[str, float, bool]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    start = time.monotonic()
    try:
        r = requests.post(MCP, json=payload,
                          headers={"Authorization": f"Bearer {TOKEN}"},
                          timeout=120)
        ok = r.status_code == 200 and "error" not in r.json().get("result", {})
        return (tool, time.monotonic() - start, ok)
    except Exception:
        return (tool, time.monotonic() - start, False)


def main() -> None:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    per_worker = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    jobs = [(t, a) for _ in range(per_worker) for t, a in TOOLS]
    print(f"load test: {workers} workers x {per_worker} rounds x {len(TOOLS)} tools = {len(jobs)} calls")
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda j: call(*j), jobs))
    wall = time.monotonic() - start

    by_tool: dict[str, list[tuple[float, bool]]] = {}
    for tool, latency, ok in results:
        by_tool.setdefault(tool, []).append((latency, ok))

    print(f"wall: {wall:.1f}s | throughput: {len(results) / wall:.1f} req/s\n")
    print(f"{'tool':<24} {'p50':>7} {'p95':>7} {'p99':>7} {'errors':>7}")
    for tool, samples in sorted(by_tool.items()):
        lats = sorted(l for l, _ in samples)
        errs = sum(1 for _, ok in samples if not ok)
        p50 = lats[len(lats) // 2]
        p95 = lats[int(len(lats) * 0.95)]
        p99 = lats[min(int(len(lats) * 0.99), len(lats) - 1)]
        print(f"{tool:<24} {p50:>6.2f}s {p95:>6.2f}s {p99:>6.2f}s {errs:>5}/{len(samples)}")
    total_errs = sum(1 for _, _, ok in results if not ok)
    print(f"\nerrors: {total_errs}/{len(results)}")
    sys.exit(1 if total_errs > len(results) * 0.05 else 0)


if __name__ == "__main__":
    main()
