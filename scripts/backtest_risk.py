#!/usr/bin/env python3
"""Backtest hotspot scores against incident history.

Labels: files touched by fix/hotfix/revert commits in the lookback window
are "incident files" — they demonstrably broke. The test: do current hotspot
scores rank those files highly?

Reports precision@N (how many of the top-N hotspot files are incident files)
and recall@N (how many incident files the top-N catches). Report-only —
threshold tuning needs more history than one run.
"""

import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import requests

API = os.environ.get("API_URL", "http://localhost:8000")
TOKEN = ""
for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
    if line.startswith("API_AUTH_TOKEN="):
        TOKEN = line.split("=", 1)[1].strip()

FIX_RE = re.compile(r"\b(fix|hotfix|revert)\b", re.IGNORECASE)


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90

    # incident labels from git history
    from app.rag.ingestion.git_ingestor import get_recent_history

    incident_files: dict[str, int] = defaultdict(int)
    for commit in get_recent_history(days):
        if FIX_RE.search(commit.get("subject") or ""):
            for f in commit.get("files") or []:
                if f.endswith(".rs"):
                    incident_files[f] += 1

    # current hotspot ranking
    r = requests.post(
        f"{API}/api/index/ingest" if False else f"{API}/health", timeout=10
    )  # warm check
    hotspots = []
    resp = requests.post(
        "http://localhost:8002/mcp" if not os.environ.get("MCP_URL") else os.environ["MCP_URL"],
        headers={"Authorization": f"Bearer {os.environ.get('MCP_AUTH_TOKEN', _mcp_token())}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "find_hotspots", "arguments": {"days": days, "limit": 50}}},
        timeout=120,
    )
    import json

    data = json.loads(resp.json()["result"]["content"][0]["text"])
    hotspots = [h["file_path"] for h in data.get("hotspots", [])]

    if not hotspots:
        print("no hotspots returned — cannot backtest")
        return 1

    print(f"backtest window: {days} days")
    print(f"incident files (fix/hotfix/revert commits): {len(incident_files)}")
    print(f"hotspot files ranked: {len(hotspots)}")
    print()
    for n in (10, 20, 50):
        top = hotspots[:n]
        hits = [f for f in top if f in incident_files]
        precision = len(hits) / len(top) if top else 0
        recall = len(hits) / len(incident_files) if incident_files else 0
        print(f"top-{n:>2}: precision {precision:5.1%}  recall {recall:5.1%}  ({len(hits)}/{len(top)} ranked files had incidents)")
    print()
    missed = [f for f in incident_files if f not in hotspots][:10]
    if missed:
        print("incident files NOT in hotspot ranking (false negatives):")
        for f in missed:
            print(f"  - {f} ({incident_files[f]} fix commits)")
    return 0


def _mcp_token() -> str:
    for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
        if line.startswith("MCP_AUTH_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


if __name__ == "__main__":
    sys.exit(main())
