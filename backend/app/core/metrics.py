import time
from threading import Lock
from typing import Any


class MetricsCollector:
    def __init__(self) -> None:
        self._lock = Lock()
        self._incident_created: int = 0
        self._incident_resolved: int = 0
        self._incident_failed: int = 0
        self._analysis_total: int = 0
        self._analysis_latency_samples: list[float] = []
        self._chat_requests: int = 0
        self._kb_sync_total: int = 0
        self._kb_sync_failed: int = 0
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._mcp_tool_calls: int = 0
        self._mcp_tool_errors: int = 0
        self._start_time: float = time.time()

    def record_incident_created(self) -> None:
        with self._lock:
            self._incident_created += 1

    def record_incident_resolved(self) -> None:
        with self._lock:
            self._incident_resolved += 1

    def record_incident_failed(self) -> None:
        with self._lock:
            self._incident_failed += 1

    def record_analysis(self, latency_seconds: float) -> None:
        with self._lock:
            self._analysis_total += 1
            self._analysis_latency_samples.append(latency_seconds)
            if len(self._analysis_latency_samples) > 1000:
                self._analysis_latency_samples = self._analysis_latency_samples[-500:]

    def record_chat_request(self) -> None:
        with self._lock:
            self._chat_requests += 1

    def record_kb_sync(self, *, success: bool = True) -> None:
        with self._lock:
            self._kb_sync_total += 1
            if not success:
                self._kb_sync_failed += 1

    def record_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    def record_cache_miss(self) -> None:
        with self._lock:
            self._cache_misses += 1

    def record_mcp_tool_call(self, *, error: bool = False) -> None:
        with self._lock:
            self._mcp_tool_calls += 1
            if error:
                self._mcp_tool_errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latencies = list(self._analysis_latency_samples)
            uptime = time.time() - self._start_time

            avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
            p50 = sorted(latencies)[len(latencies) // 2] if latencies else 0.0
            p95 = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0.0
            p99 = sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0.0

            total_cache = self._cache_hits + self._cache_misses
            cache_hit_rate = self._cache_hits / total_cache if total_cache > 0 else 0.0

            return {
                "uptime_seconds": round(uptime, 1),
                "incidents": {
                    "created": self._incident_created,
                    "resolved": self._incident_resolved,
                    "failed": self._incident_failed,
                    "throughput_per_minute": round(self._incident_created / (uptime / 60), 2) if uptime > 0 else 0.0,
                },
                "analysis": {
                    "total": self._analysis_total,
                    "avg_latency_seconds": round(avg_latency, 3),
                    "p50_latency_seconds": round(p50, 3),
                    "p95_latency_seconds": round(p95, 3),
                    "p99_latency_seconds": round(p99, 3),
                },
                "chat": {
                    "total_requests": self._chat_requests,
                },
                "kb_sync": {
                    "total": self._kb_sync_total,
                    "failed": self._kb_sync_failed,
                },
                "cache": {
                    "hits": self._cache_hits,
                    "misses": self._cache_misses,
                    "hit_rate": round(cache_hit_rate, 4),
                },
                "mcp": {
                    "tool_calls": self._mcp_tool_calls,
                    "tool_errors": self._mcp_tool_errors,
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._incident_created = 0
            self._incident_resolved = 0
            self._incident_failed = 0
            self._analysis_total = 0
            self._analysis_latency_samples.clear()
            self._chat_requests = 0
            self._kb_sync_total = 0
            self._kb_sync_failed = 0
            self._cache_hits = 0
            self._cache_misses = 0
            self._mcp_tool_calls = 0
            self._mcp_tool_errors = 0
            self._start_time = time.time()


metrics_collector = MetricsCollector()
