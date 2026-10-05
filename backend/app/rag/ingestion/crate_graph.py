"""Crate-level dependency graph from Cargo.toml files.

Workspace members plus their path dependencies (direct and
workspace-inherited) form the crate graph. Cross-crate changes are riskier
than intra-crate ones: a crate with many internal dependents sits on a wider
blast radius. Registry dependencies (crates.io) are external and ignored.

Parsed with tomllib, cached 10 minutes — Cargo.tomls change rarely and the
graph tools hit this on every call.
"""

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE: tuple[float, dict] = (0.0, {})
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600.0


def _parse_cargo_toml(path: Path) -> dict:
    import tomllib

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        logger.warning("crate_graph: failed to parse %s: %s", path, exc)
        return {}


def _member_name(manifest: dict, fallback: str) -> str:
    pkg = manifest.get("package") or {}
    return pkg.get("name") or fallback


def _path_deps(manifest: dict, workspace_deps: dict[str, str], crate_dir: Path) -> list[str]:
    """Resolve this crate's internal dependencies to crate NAMES.

    Handles both `foo = { path = "../foo" }` and workspace inheritance
    `foo = { workspace = true }` (resolved via [workspace.dependencies]).
    """
    names: set[str] = set()
    for section in ("dependencies", "dev-dependencies"):
        for dep_name, spec in (manifest.get(section) or {}).items():
            if not isinstance(spec, dict):
                continue  # registry dep ("foo = "1.0"")
            if "path" in spec:
                dep_dir = (crate_dir / spec["path"]).resolve()
                dep_manifest = _parse_cargo_toml(dep_dir / "Cargo.toml")
                if dep_manifest:
                    names.add(_member_name(dep_manifest, dep_name))
            elif spec.get("workspace") is True and dep_name in workspace_deps:
                # workspace.dependencies entries carry the path (or are registry deps)
                wspec = workspace_deps[dep_name]
                if isinstance(wspec, dict) and "path" in wspec:
                    dep_dir = (crate_dir / wspec["path"]).resolve()
                    dep_manifest = _parse_cargo_toml(dep_dir / "Cargo.toml")
                    if dep_manifest:
                        names.add(_member_name(dep_manifest, dep_name))
    return sorted(names)


def load_crate_graph(repository_path: str | None = None) -> dict[str, dict]:
    """{crate_name: {"path": str, "dependencies": [names], "dependents": [names]}}"""
    global _CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        if now - _CACHE[0] < _CACHE_TTL and _CACHE[1]:
            return _CACHE[1]

    from app.rag.ingestion.git_ingestor import resolve_repository_path

    repo = Path(resolve_repository_path(repository_path))
    root_manifest = _parse_cargo_toml(repo / "Cargo.toml")

    members: list[str] = list(root_manifest.get("workspace", {}).get("members", []))
    workspace_deps = {
        k: v for k, v in (root_manifest.get("workspace", {}).get("dependencies") or {}).items()
        if isinstance(v, dict)
    }

    graph: dict[str, dict] = {}
    for member in members:
        crate_dir = (repo / member).resolve()
        manifest = _parse_cargo_toml(crate_dir / "Cargo.toml")
        if not manifest:
            continue
        name = _member_name(manifest, member.replace("/", "-"))
        graph[name] = {
            "path": member,
            "manifest_dir": str(crate_dir),
            "dependencies": _path_deps(manifest, workspace_deps, crate_dir),
            "dependents": [],
        }

    for name, info in graph.items():
        for dep in info["dependencies"]:
            if dep in graph:
                graph[dep]["dependents"].append(name)

    with _CACHE_LOCK:
        _CACHE = (now, graph)
    return graph


def crate_edges(graph: dict[str, dict]) -> list[tuple[str, str]]:
    """Directed (dependent -> dependency) edges between workspace crates."""
    edges: set[tuple[str, str]] = set()
    for name, info in graph.items():
        for dep in info["dependencies"]:
            if dep in graph and dep != name:
                edges.add((name, dep))
    return sorted(edges)
