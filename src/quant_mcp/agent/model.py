"""Model configuration for the registry-based coding agent."""
from __future__ import annotations

import os


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except ValueError:
        return default


def build_model(timeout_seconds: int):
    from smolagents import OpenAIModel

    api_key = os.getenv("SMOLAGENTS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("SMOLAGENTS_API_KEY or DEEPSEEK_API_KEY is not set")
    model_id = os.getenv("SMOLAGENTS_MODEL_ID") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    options = {}
    if model_id.startswith("deepseek-"):
        thinking = os.getenv("DEEPSEEK_THINKING", "disabled").lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_THINKING must be enabled or disabled")
        options["extra_body"] = {"thinking": {"type": thinking}}
    return OpenAIModel(
        model_id=model_id,
        api_base=os.getenv("SMOLAGENTS_API_BASE") or os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
        api_key=api_key,
        client_kwargs={"timeout": min(env_int("SMOLAGENTS_MODEL_TIMEOUT_SECONDS", 120), timeout_seconds),
                       "max_retries": 1},
        **options,
    )


def instrument_model(model, run):
    """Instrument this instance's public generation methods without replacing it.

    Both ordinary and streaming generation get OTel spans and live progress.
    No assumptions about planning prompts or stop sequences are needed.
    """
    import functools
    import itertools
    import time
    from contextlib import contextmanager

    from quant_mcp.progress import emit_event
    from quant_mcp.tracing import operation, set_outcome

    counter = itertools.count(1)
    originals = getattr(model, "_quant_mcp_original_generation", None)
    if originals is None:
        originals = {name: getattr(model, name) for name in ("generate", "generate_stream")
                     if callable(getattr(model, name, None))}
        model._quant_mcp_original_generation = originals

    @contextmanager
    def generation(streaming):
        with operation("model.generate", **{"gen_ai.request.model": str(model.model_id),
                                           "quant_mcp.streaming": streaming}):
            fields = {"model_call": next(counter), "streaming": streaming}
            started = time.monotonic()
            status, error_type = "failed", None
            emit_event(run, "model_call_started", **fields)
            try:
                yield
                status = "success"
            except BaseException as exc:
                error_type = type(exc).__name__
                if isinstance(exc, GeneratorExit):
                    status = "cancelled"
                raise
            finally:
                set_outcome(status)
                emit_event(run, "model_call_finished", **fields, status=status,
                           error_type=error_type,
                           duration_ms=round((time.monotonic() - started) * 1000))

    @functools.wraps(originals["generate"])
    def generate(*args, **kwargs):
        with generation(False):
            return originals["generate"](*args, **kwargs)

    model.generate = generate
    if "generate_stream" in originals:
        @functools.wraps(originals["generate_stream"])
        def generate_stream(*args, **kwargs):
            with generation(True):
                yield from originals["generate_stream"](*args, **kwargs)
        model.generate_stream = generate_stream
    return model


def restore_model(model):
    """Remove this task's instrumentation before a supplied model is reused."""
    originals = getattr(model, "_quant_mcp_original_generation", None)
    if originals is not None:
        for name, method in originals.items():
            setattr(model, name, method)
        del model._quant_mcp_original_generation
