import json
from pathlib import Path

import pytest

from quant_mcp.pi_execution import execute_script


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src" / "demo_library").mkdir(parents=True)
    (repo / "src" / "demo_library" / "__init__.py").write_text('''
class Engine:
    def __init__(self):
        self.multiplier = 3
    def calculate(self, value):
        return value * self.multiplier
# Import-time calculations must not count as task reuse.
Engine().calculate(9)
''')
    # Exercise the legacy configuration independently of a user's active YAML map.
    monkeypatch.delenv("QUANT_MCP_CODEBASES_FILE", raising=False)
    monkeypatch.setenv("QUANT_MCP_REPO_ROOT", str(repo))
    monkeypatch.setenv("QUANT_MCP_LIBRARY_MODULES", "demo_library")
    task = tmp_path / "task"
    task.mkdir()
    (task / "prompt.txt").write_text("calculate a value with the existing library")
    return task


def write_script(workspace, action, metric="42"):
    (workspace / "analysis.py").write_text(f'''import json
from pathlib import Path
from demo_library import Engine
{action}
Path('result.json').write_text(json.dumps({{
 'status':'success', 'summary':'calculated', 'metrics':{{'value':{metric}}},
 'assumptions':[], 'artifacts':[]
}}))
''')


def test_class_method_execution_is_recorded(workspace):
    write_script(workspace, "value = Engine().calculate(14)", "value")
    outcome = execute_script(workspace, 10)
    assert outcome["status"] == "success", outcome
    assert outcome["result"]["metrics"]["value"] == 42
    assert [c["symbol"] for c in outcome["library_calls"]] == ["Engine.calculate"]
    assert outcome["library_calls"][0]["count"] == 1
    assert any(a["path"].endswith("library_calls.json") for a in outcome["result"]["artifacts"])


@pytest.mark.parametrize("action", ["pass", "engine = Engine()", "if False: Engine().calculate(14)", "def substitute(x): return x * 3\nvalue = substitute(14)"])
def test_import_constructor_dead_code_and_reimplementation_are_not_reuse(workspace, action):
    write_script(workspace, action)
    outcome = execute_script(workspace, 10)
    assert outcome["status"] == "failed"
    assert "no registered library" in outcome["error"]
    assert not (workspace / "result.json").exists()


def test_failure_returns_traceback_and_next_attempt_repairs(workspace):
    write_script(workspace, "value = Engine().calculate(14)\nraise ValueError('bad report')", "value")
    first = execute_script(workspace, 10)
    assert first["status"] == "failed"
    assert "ValueError: bad report" in first["log"]
    assert (workspace / "attempts/attempt_01/analysis.py").exists()
    write_script(workspace, "value = Engine().calculate(14)", "value")
    assert execute_script(workspace, 10)["status"] == "success"


def test_bad_artifacts_are_rejected(workspace):
    write_script(workspace, "value = Engine().calculate(14)", "value")
    script = workspace / "analysis.py"
    script.write_text(script.read_text().replace("'artifacts':[]", "'artifacts':['missing.csv']"))
    assert execute_script(workspace, 10)["status"] == "failed"
