import json
import multiprocessing
from pathlib import Path

from quant_mcp.progress import RunContext, emit_event, flush_progress_logs


def append_events(root, request_id):
    trace = RunContext(Path(root), request_id, "direct", "example")
    for index in range(50):
        emit_event(trace, "function_call_started", call_id=str(index))
    flush_progress_logs()


def test_concurrent_processes_append_complete_records(tmp_path):
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=append_events, args=(str(tmp_path), str(i))) for i in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join()
            raise AssertionError("Trace writer did not finish")
        assert process.exitcode == 0
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(records) == len({r["event_id"] for r in records}) == 150
    for request_id in ("0", "1", "2"):
        assert [r["call_id"] for r in records if r["request_id"] == request_id] == [str(i) for i in range(50)]


def test_spans_propagate_across_server_agent_and_function_processes(tmp_path):
    """An app-owned SDK hook runs in fresh workers and exports one trace tree."""
    import os
    import subprocess
    import sys

    (tmp_path / 'otel_setup.py').write_text('''import os
from pathlib import Path
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

def configure():
    folder = Path(os.environ["TEST_TRACE_DIR"])
    stream = (folder / (str(os.getpid()) + ".jsonl")).open("a")
    provider = TracerProvider(resource=Resource.create({"service.name":"test-framework"}))
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(
        out=stream, formatter=lambda span: span.to_json(indent=None) + "\\n")))
    trace.set_tracer_provider(provider)
    # Scripted responses exercise CodeAgent without a network/model API call.
    from smolagents.models import Model, ChatMessage
    from quant_mcp.agent import registered
    class ModelStub(Model):
        def __init__(self):
            super().__init__(model_id="test-model")
        def generate(self, messages, **kwargs):
            return ChatMessage(role="assistant", content="""<code>
result = value(number=21)
write_artifact(filename="value.txt", content=str(result))
final_answer({"task_type":"analysis", "summary":"Calculated", "metrics":{"value":result}})
</code>""")
    registered.build_model = lambda timeout: ModelStub()
''')
    (tmp_path / 'office.py').write_text('''from opentelemetry import trace

def value(number: int) -> int:
    """Double a number."""
    with trace.get_tracer("office").start_as_current_span("office.calculation"):
        return number * 2

def fail(secret: str) -> int:
    """Exercise sanitized telemetry errors."""
    raise ValueError(secret)
''')
    (tmp_path / 'probe.py').write_text('''import asyncio
from pathlib import Path
from office import value, fail
from quant_mcp.framework import MCPFramework
from quant_mcp.tracing import flush_telemetry

app = MCPFramework("probe", artifacts_dir=Path(__file__).parent / "artifacts",
                   telemetry_setup="otel_setup:configure")
app.expose(value)
app.expose(fail)
async def exercise():
    results = await asyncio.gather(
        app.mcp.call_tool("value", {"number":21}),
        app.mcp.call_tool("run_coding_task", {"prompt":"SECRET_PROMPT", "timeout_seconds":30}),
    )
    assert results[0][1] == {"result":42}
    assert results[1][1]["metrics"] == {"value":42}
    for name, args in [("fail", {"secret":"SECRET_ERROR"}), ("value", {"number":"SECRET_ARGUMENT"})]:
        try:
            await app.mcp.call_tool(name, args)
        except Exception:
            pass
        else:
            raise AssertionError("Expected a failure")
asyncio.run(exercise())
flush_telemetry()
''')
    spans_dir = tmp_path / 'spans'
    spans_dir.mkdir()
    env = dict(os.environ, TEST_TRACE_DIR=str(spans_dir), SMOLAGENTS_PLANNING_INTERVAL='0')
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), str(Path(__file__).resolve().parents[1] / 'src')])
    subprocess.run([sys.executable, str(tmp_path / 'probe.py')], env=env,
                   capture_output=True, text=True, check=True, timeout=45)
    content = '\n'.join(path.read_text() for path in spans_dir.glob('*.jsonl'))
    spans = [json.loads(line) for line in content.splitlines() if line]
    roots = [s for s in spans if s['name'] == 'mcp.tool.call']
    assert len(roots) == 4
    assert len({s['context']['trace_id'] for s in roots}) == 4
    by_id = {s['context']['span_id']: s for s in spans}
    assert all(s['parent_id'] in by_id for s in spans if s['name'] != 'mcp.tool.call')
    assert all(by_id[s['parent_id']]['context']['trace_id'] == s['context']['trace_id']
               for s in spans if s['parent_id'])
    agent_root = next(s for s in roots if s['attributes']['quant_mcp.mode'] == 'coding_agent')
    agent_spans = [s for s in spans if s['context']['trace_id'] == agent_root['context']['trace_id']]
    assert {s['name'] for s in agent_spans} == {
        'mcp.tool.call', 'task.worker', 'agent.run', 'model.generate',
        'function.call', 'library.function', 'office.calculation',
    }
    assert len(list(spans_dir.glob('*.jsonl'))) >= 4  # Actual independently configured processes.
    assert len([s for s in roots if s['status']['status_code'] == 'ERROR']) == 2
    assert all(secret not in content for secret in ('SECRET_PROMPT', 'SECRET_ERROR', 'SECRET_ARGUMENT'))
    events = [json.loads(line) for line in (tmp_path / 'artifacts/events.jsonl').read_text().splitlines()]
    for event in events:
        assert '0x' + event['span_id'] in by_id
        assert by_id['0x' + event['span_id']]['context']['trace_id'] == '0x' + event['trace_id']


def test_model_streams_and_failures_use_standard_spans(tmp_path, monkeypatch):
    import pytest
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from quant_mcp import tracing
    from quant_mcp.agent.model import instrument_model

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, 'tracer', provider.get_tracer('test'))
    run = RunContext(tmp_path, 'request', 'coding_agent', 'run_coding_task')

    class Model:
        model_id = 'scripted'
        def generate(self):
            raise ValueError('SECRET_ERROR')
        def generate_stream(self):
            yield 'one'
            yield 'two'

    model = Model()
    assert instrument_model(model, run) is model
    with tracing.operation('parent'):
        assert list(model.generate_stream()) == ['one', 'two']
        with pytest.raises(ValueError):
            model.generate()
        stream = model.generate_stream()
        assert next(stream) == 'one'
        stream.close()
    flush_progress_logs()
    spans = exporter.get_finished_spans()
    assert [s.attributes.get('quant_mcp.outcome') for s in spans[:-1]] == ['success', 'failed', 'cancelled']
    assert all(s.parent.span_id == spans[-1].context.span_id for s in spans[:-1])
    assert 'SECRET_ERROR' not in '\n'.join(s.to_json() for s in spans)
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert [e['status'] for e in events if e['event'] == 'model_call_finished'] == ['success', 'failed', 'cancelled']
    provider.shutdown()


def test_slow_progress_sink_does_not_block_event_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    from quant_mcp import progress

    entered, release = threading.Event(), threading.Event()
    original = progress._append

    def slow_append(*args):
        entered.set()
        assert release.wait(timeout=5)
        original(*args)

    monkeypatch.setattr(progress, '_append', slow_append)
    run = RunContext(tmp_path, 'request', 'direct', 'example')

    async def exercise():
        emit_event(run, 'request_started')
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # The disk writer is still blocked, but other coroutines run.
            await asyncio.sleep(0)
            assert not (tmp_path / 'events.jsonl').exists()
        finally:
            release.set()
        await asyncio.to_thread(flush_progress_logs)

    asyncio.run(exercise())
    assert json.loads((tmp_path / 'events.jsonl').read_text())['event'] == 'request_started'


def test_direct_framework_works_without_importing_the_optional_sdk(tmp_path):
    import os
    import subprocess
    import sys

    (tmp_path / 'office.py').write_text('def value(number: int) -> int:\n    return number * 2\n')
    probe = '''import asyncio
import importlib.abc
import json
import sys
from pathlib import Path

class BlockSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(("opentelemetry.sdk", "opentelemetry.exporter")):
            raise AssertionError("Optional SDK imported: " + fullname)
sys.meta_path.insert(0, BlockSDK())
from office import value
from quant_mcp.framework import MCPFramework
app = MCPFramework("api-only", artifacts_dir=Path.cwd() / "artifacts")
app.expose(value)
assert asyncio.run(app.mcp.call_tool("value", {"number":21}))[1] == {"result":42}
events = [json.loads(line) for line in (app.artifacts.root / "events.jsonl").read_text().splitlines()]
assert len(events) == 4
assert all(e["trace_id"] is None and e["span_id"] is None for e in events)
assert not any(key.startswith("opentelemetry.sdk") for key in sys.modules)
'''
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), str(Path(__file__).resolve().parents[1] / 'src')])
    subprocess.run([sys.executable, '-c', probe], cwd=tmp_path, env=env,
                   capture_output=True, text=True, check=True, timeout=15)
