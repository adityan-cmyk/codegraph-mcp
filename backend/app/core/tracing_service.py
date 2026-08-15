from contextlib import contextmanager
from typing import Generator

from app.core.config import settings


class _NoOpSpan:
    def update(self, **_: object) -> None: ...
    def add_event(self, **_: object) -> None: ...
    def update_end_time(self) -> None: ...


_no_op_span = _NoOpSpan()


class TracingService:
    def __init__(self) -> None:
        self._client = None
        self._enabled = settings.langfuse_enabled
        if self._enabled and settings.langfuse_public_key and settings.langfuse_secret_key:
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
            except ImportError:
                self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled and self._client is not None

    @contextmanager
    def trace(self, name: str, *, metadata: dict | None = None) -> Generator[object, None, None]:
        if not self.is_enabled:
            yield _no_op_span
            return

        trace = self._client.trace(name=name, metadata=metadata)
        try:
            yield trace
        finally:
            trace.update_end_time()

    @contextmanager
    def span(
        self, name: str, *, trace_id: str | None = None, metadata: dict | None = None
    ) -> Generator[object, None, None]:
        if not self.is_enabled or not self._client:
            yield _no_op_span
            return

        span_obj = self._client.span(name=name, trace_id=trace_id, metadata=metadata)
        try:
            yield span_obj
        finally:
            span_obj.update_end_time()

    @contextmanager
    def generation(
        self, name: str, *, trace_id: str | None = None, model: str | None = None, input: dict | None = None
    ) -> Generator[object, None, None]:
        if not self.is_enabled or not self._client:
            yield _no_op_span
            return

        gen = self._client.generation(name=name, trace_id=trace_id, model=model, input=input)
        try:
            yield gen
        finally:
            gen.update_end_time()

    def update_generation(self, generation: object, output: dict | None = None, usage: dict | None = None) -> None:
        if hasattr(generation, "update"):
            kwargs: dict = {}
            if output is not None:
                kwargs["output"] = output
            if usage is not None:
                kwargs["usage"] = usage
            if kwargs:
                generation.update(**kwargs)


tracing_service = TracingService()
