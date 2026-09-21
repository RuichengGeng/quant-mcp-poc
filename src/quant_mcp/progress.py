"""Live progress via Python logging, independent of OpenTelemetry exporters."""
from __future__ import annotations

import atexit
import fcntl
import json
import logging
import logging.handlers
import os
import queue
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace

logger = logging.getLogger("quant_mcp.audit")
_listener = None
_setup_lock = threading.Lock()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_task_id() -> str:
    return f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"


@dataclass
class RunContext:
    root: Path
    request_id: str
    mode: str
    tool: str
    task_id: str | None = None
    telemetry_setup: str | None = None
    status: str | None = None
    task_type: str | None = None

    def payload(self) -> dict:
        return {**asdict(self), "root": str(self.root)}

    @classmethod
    def from_payload(cls, payload: dict) -> RunContext:
        return cls(**{**payload, "root": Path(payload["root"])})


current_run: ContextVar[RunContext | None] = ContextVar("quant_mcp_run", default=None)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.write(line + "\n")
            stream.flush()
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class _ProgressFiles(logging.Handler):
    """The file sink runs on a QueueListener thread, never the MCP event loop."""

    def emit(self, record):
        for filename in record.progress_paths:
            try:
                _append(Path(filename), record.getMessage())
            except OSError as exc:
                # A different logger avoids recursively enqueueing sink failures.
                logging.getLogger("quant_mcp.logging").warning(
                    "Could not write lifecycle log (%s)", type(exc).__name__)


def setup_progress_logging() -> None:
    global _listener
    with _setup_lock:
        if _listener is not None:
            return
        records = queue.Queue()
        handler = logging.handlers.QueueHandler(records)
        handler.addFilter(lambda record: hasattr(record, "progress_paths"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        _listener = logging.handlers.QueueListener(records, _ProgressFiles())
        _listener.start()
        atexit.register(flush_progress_logs)


def flush_progress_logs() -> None:
    if _listener is not None:
        _listener.queue.join()


def emit_event(run: RunContext, event: str, **fields) -> None:
    setup_progress_logging()
    span = trace.get_current_span()
    for key, value in (("request_id", run.request_id), ("mode", run.mode),
                       ("tool", run.tool), ("task_id", run.task_id)):
        if value is not None:
            span.set_attribute("quant_mcp." + key, value)
    span_context = span.get_span_context()
    record = {
        "timestamp": timestamp(), "event_id": uuid.uuid4().hex,
        "event": event, "request_id": run.request_id,
        "mode": run.mode, "tool": run.tool, "task_id": run.task_id,
        "trace_id": format(span_context.trace_id, "032x") if span_context.is_valid else None,
        "span_id": format(span_context.span_id, "016x") if span_context.is_valid else None,
        "pid": os.getpid(), **fields,
    }
    paths = [str(run.root / "events.jsonl")]
    if run.task_id is not None:
        paths.append(str(run.root / run.task_id / "events.jsonl"))
    logger.info(json.dumps(record, ensure_ascii=False), extra={"progress_paths": paths})
