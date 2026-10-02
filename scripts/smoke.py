#!/usr/bin/env python3
"""Post-deploy smoke test — functional invariants, not just liveness.

Every failure class from the 2026-10-02 session is pinned here:
  - make_decision dead (schema/code drift, lock starvation)
  - semantic scores flat 1.0 (None-distance coercion)
  - stats disagreeing between tools
  - tools 500ing on real input

Run against the LIVE server (localhost). Exit 0 = all invariants hold.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

API = "http://localhost:8000"
MCP = "http://localhost:8002/mcp"


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


API_TOKEN = _env("API_AUTH_TOKEN")
MCP_TOKEN = _env("MCP_AUTH_TOKEN")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def mcp_call(tool: str, arguments: dict, timeout: float = 120.0) -> dict:
    r = requests.post(
        MCP,
        headers={"Authorization": f"Bearer {MCP_TOKEN}", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        return {"__rpc_error__": payload["error"]}
    return json.loads(payload["result"]["content"][0]["text"])


def _probe_symbol() -> str:
    """Resolve a real symbol at runtime — hardcoded ids break every reindex."""
    ss = mcp_call("search_symbols", {"query": "wallet_closure", "limit": 5})
    for s in ss.get("symbols", []):
        sid = s.get("symbol_id", "")
        if sid and "::fn" not in sid:
            return sid
    raise RuntimeError(f"no probe symbol found: {ss}")


def main() -> int:
    # 1. Liveness + backend deps
    r = requests.get(f"{API}/health", timeout=10)
    check("health endpoint", r.status_code == 200)

    # 2. Decision model self-report
    r = requests.get(f"{API}/api/health/decision-model",
                     headers={"Authorization": f"Bearer {API_TOKEN}"}, timeout=10)
    dm = r.json() if r.status_code == 200 else {}
    check("decision model available", dm.get("available") is True,
          str(dm.get("reason", ""))[:80])

    # 3. make_decision actually answers (noul ping) — busy is transient, retry
    md = {}
    for attempt in range(3):
        t0 = time.time()
        md = mcp_call("make_decision", {
            "state": "smoke test ping",
            "questions": {"alive": {"type": "noul", "instructions": "Is the system alive?"}},
        })
        if "error" not in md or "busy" not in str(md.get("error", "")).lower():
            break
        time.sleep(25)
    elapsed = time.time() - t0
    answers = md.get("answers", {})
    check("make_decision answers noul",
          "alive" in answers and isinstance(answers["alive"].get("noul"), (int, float)),
          f"{elapsed:.0f}s" + (f" err={md.get('error', '')[:60]}" if "error" in md else ""))

    # 4. Semantic scores are not flat (the None-distance 1.0 bug)
    ss = mcp_call("semantic_search", {"query": "wallet closure settlement", "limit": 5})
    results = ss.get("results", [])
    scores = [m["score"] for m in results if isinstance(m.get("score"), (int, float))]
    spread = (max(scores) - min(scores)) if len(scores) >= 2 else 0
    check("semantic scores have spread", len(scores) >= 1 and not (spread == 0 and len(scores) > 1),
          f"scores={[round(s, 3) for s in scores[:5]]}")
    check("semantic scores not all 1.0", not (scores and all(s == 1.0 for s in scores)))

    # 5. Graph stats internally consistent
    gs = mcp_call("get_graph_stats", {})
    nodes = gs.get("graph_nodes", 0)
    total = gs.get("total_symbols_all_chunks", nodes)
    edges = gs.get("graph_edges", 0)
    check("graph stats consistent", nodes > 0 and edges > 0 and nodes <= total,
          f"nodes={nodes} edges={edges} total={total} excluded={gs.get('excluded_from_graph')}")

    # 6. Index meta exposes drift + coverage
    im = mcp_call("get_index_meta", {})
    check("index meta has coverage + commits_behind",
          "coverage" in im and "commits_behind" in im,
          f"commits_behind={im.get('commits_behind')}")

    # 7. Traverse summary_only returns a summary
    probe = _probe_symbol()
    tv = mcp_call("traverse_graph", {"symbol_id": probe, "depth": 2, "summary_only": True})
    check("traverse summary_only works", "error" not in tv and ("levels" in tv or "total_symbols" in tv or "summary" in str(tv)[:200]),
          str(tv.get("error", ""))[:60])

    # 8. Metal detector returns observations
    fw = mcp_call("find_warnings_in_blast_radius", {"symbol_id": probe, "radius": 1})
    check("find_warnings_in_blast_radius works", "error" not in fw and "warnings_total" in fw,
          f"warnings={fw.get('warnings_total', '?')} scanned={fw.get('symbols_scanned', '?')}")

    # 9. Multi-word search
    ms = mcp_call("search_symbols_enhanced", {"query": "closure wallet", "limit": 5})
    check("multi-word search returns results",
          len(ms.get("results", ms.get("symbols", []))) > 0 or len(ms.get("file_matches", [])) > 0)

    # 10. Feedback pipeline accepts submissions (then is immediately gated)
    fb = requests.post(
        f"{API}/api/feedback/ai",
        headers={"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"},
        json={"pr_context": "smoke", "tools_called": [], "results_used": [],
              "results_expected": "smoke test verifies the submission path works",
              "quality_rating": 3, "improvement_suggestions": "smoke ping — safe to reject"},
        timeout=30,
    )
    check("ai_feedback submission accepted", fb.status_code in (200, 201, 202),
          f"status={fb.status_code}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} SMOKE FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
