import json

import pytest

from quant_mcp.agent import SmolagentsCodingAgent
from quant_mcp.runner import build_agent, run_quant_task


SUCCESS_SCRIPT = """import json
from pathlib import Path
from quant_mcp.pricing import price_option_t
premium = price_option_t('put', 100, 110, 0.25, 0.25, 0.04)
count = Path('execution_count.txt')
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
Path('result.json').write_text(json.dumps({
    'status': 'success', 'task_type': 'pricing', 'summary': 'priced',
    'metrics': {'premium': premium}, 'assumptions': [], 'artifacts': []
}))
"""


@pytest.fixture
def model_stub(monkeypatch):
    from smolagents.models import ChatMessage, Model

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'deepseek-v4-flash')
    monkeypatch.delenv('DEEPSEEK_THINKING', raising=False)
    monkeypatch.setenv('SMOLAGENTS_MAX_STEPS', '2')
    monkeypatch.setenv('SMOLAGENTS_MODEL_TIMEOUT_SECONDS', '120')

    class ScriptModel(Model):
        def __init__(self, scripts):
            super().__init__(model_id='test-model')
            self.scripts = iter(scripts)
            self.requests = []

        def generate(self, messages, **kwargs):
            self.requests.append(str(messages))
            return ChatMessage(role='assistant', content='<code>\n' + next(self.scripts) + '\n</code>')

    def install(scripts):
        model = ScriptModel(scripts)
        def factory(**kwargs):
            assert kwargs['client_kwargs']['timeout'] == 120
            assert kwargs['client_kwargs']['max_retries'] == 1
            assert kwargs['extra_body'] == {'thinking': {'type': 'disabled'}}
            return model
        monkeypatch.setattr('smolagents.OpenAIModel', factory)
        return model
    return install


def test_build_agent_selects_smolagents(monkeypatch):
    monkeypatch.setenv('QUANT_MCP_AGENT', 'smolagents')
    assert isinstance(build_agent(), SmolagentsCodingAgent)
    assert build_agent().auto_approve


def test_real_codeagent_repairs_traceback_and_does_not_reexecute(model_stub, monkeypatch):
    model = model_stub([
        "from quant_mcp.pricing import calc_greeks_t\ng = calc_greeks_t('call', 8, 11, 0.25, 0.25, 0.04)\nprint(g['premium'])",
        SUCCESS_SCRIPT,
    ])
    def forbidden_review(*args):
        raise AssertionError('MCP must not ask for review')
    monkeypatch.setattr('quant_mcp.agent.smolagents._terminal_approval', forbidden_review)
    task = run_quant_task('price a put', agent=SmolagentsCodingAgent(auto_approve=True))
    assert task.result['status'] == 'success', task.result
    assert task.result['metrics']['premium'] == pytest.approx(10.823125337158686)
    assert (task.task_dir / 'execution_count.txt').read_text() == '1'
    assert len(model.requests) == 2
    assert "KeyError" in model.requests[1] and "premium" in model.requests[1]
    assert (task.task_dir / 'attempts/attempt_01/run.log').exists()
    saved = json.loads((task.task_dir / 'smolagents_result.json').read_text())
    assert saved['actions'] == 2
    assert saved['status'] == 'success'


def test_step_exhaustion_does_not_request_a_final_summary(model_stub):
    model = model_stub(["raise RuntimeError('broken')"] * 2)
    task = run_quant_task('write a script', agent=SmolagentsCodingAgent(auto_approve=True))
    assert task.result['status'] == 'failed'
    assert 'broken' in task.result['summary']
    assert len(model.requests) == 2


def test_rejected_script_is_not_executed(model_stub):
    model_stub([SUCCESS_SCRIPT])
    task = run_quant_task('price a put', agent=SmolagentsCodingAgent(approval_callback=lambda *_: False))
    assert task.result['status'] == 'failed'
    assert 'rejected' in task.result['summary']
    assert not (task.task_dir / 'execution_count.txt').exists()


@pytest.mark.parametrize('invalid_value, expected_error', [
    ("['report.csv']", 'artifacts must be a list of objects'),
    ("'report.csv'", 'artifacts must be a list of objects'),
])
def test_real_codeagent_repairs_malformed_artifact_schema(model_stub, invalid_value, expected_error):
    invalid = SUCCESS_SCRIPT.replace("'artifacts': []", "'artifacts': " + invalid_value)
    model = model_stub([invalid, SUCCESS_SCRIPT])
    task = run_quant_task('price a put', agent=SmolagentsCodingAgent(auto_approve=True))
    assert task.result['status'] == 'success', task.result
    assert expected_error in model.requests[1]
    assert len(model.requests) == 2
