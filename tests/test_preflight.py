import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def application(tmp_path):
    (tmp_path / 'office.py').write_text('''from pathlib import Path

def value(number: int) -> int:
    """Return a number; pre-flight must not execute this."""
    Path(__file__).with_name("FUNCTION_WAS_CALLED").touch()
    return number
''')
    server = tmp_path / 'server.py'
    server.write_text('''from pathlib import Path
from office import value
from quant_mcp.framework import MCPFramework
from quant_mcp.config import load_env_file
ROOT = Path(__file__).parent
load_env_file(ROOT / ".env")
app = MCPFramework("preflight-test", artifacts_dir=ROOT / "artifacts")
app.expose(value)
if __name__ == "__main__":
    app.run()
''')
    return server


def run_check(server, *options):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    result = subprocess.run([sys.executable, '-m', 'quant_mcp.preflight', str(server), '--json', *options],
                            env=env, cwd=server.parent, capture_output=True, text=True, timeout=25)
    return result, json.loads(result.stdout)


def test_preflight_protocol_worker_imports_and_no_business_calls(application):
    result, report = run_check(application)
    assert result.returncode == 0 and report['ok'], report
    assert next(c for c in report['checks'] if c['name'] == 'mcp.stdio')['status'] == 'PASS'
    assert next(c for c in report['checks'] if c['name'] == 'function.import')['status'] == 'PASS'
    assert next(c for c in report['checks'] if c['name'] == 'agent.credentials')['status'] == 'WARN'
    assert not application.with_name('FUNCTION_WAS_CALLED').exists()
    assert list(application.with_name('artifacts').iterdir()) == []


def test_preflight_can_require_agent_credentials(application):
    result, report = run_check(application, '--require-agent')
    assert result.returncode == 1 and not report['ok']
    assert next(c for c in report['checks'] if c['name'] == 'agent.credentials')['status'] == 'FAIL'
    # Agent readiness does not hide the independent MCP transport check.
    assert next(c for c in report['checks'] if c['name'] == 'mcp.stdio')['status'] == 'PASS'


def test_preflight_loads_application_env_without_leaking_values(application):
    application.with_name('.env').write_text('SMOLAGENTS_API_KEY=SECRET_API_KEY\nSMOLAGENTS_API_BASE=https://secret.invalid/v1\n')
    result, report = run_check(application, '--require-agent')
    assert result.returncode == 0, report
    assert next(c for c in report['checks'] if c['name'] == 'agent.credentials')['status'] == 'PASS'
    assert 'SECRET_API_KEY' not in result.stdout + result.stderr
    assert 'secret.invalid' not in result.stdout + result.stderr


@pytest.mark.parametrize('startup', [
    'import a_missing_preflight_dependency\n',
    'raise RuntimeError("SECRET_STARTUP_ERROR")\n',
])
def test_preflight_reports_startup_failures_safely(application, startup):
    application.write_text(startup)
    result, report = run_check(application)
    assert result.returncode == 1 and not report['ok']
    row = next(c for c in report['checks'] if c['name'] == 'application')
    assert row['status'] == 'FAIL' and row['fix']
    assert 'SECRET_STARTUP_ERROR' not in result.stdout + result.stderr
    assert (Path(report['diagnostics_dir']) / 'inspection.log').exists()


def test_preflight_rejects_stdout_noise_without_leaking_it(application):
    application.write_text(application.read_text().replace('    app.run()', '    print("SECRET_STDOUT_NOISE", flush=True)\n    app.run()'))
    result, report = run_check(application)
    assert result.returncode == 1, report
    assert next(c for c in report['checks'] if c['name'] == 'mcp.stdio')['status'] == 'FAIL'
    assert 'SECRET_STDOUT_NOISE' not in result.stdout + result.stderr
    assert 'SECRET_STDOUT_NOISE' in (Path(report['diagnostics_dir']) / 'client.log').read_text()


def test_preflight_reports_unwritable_artifacts(application):
    application.with_name('artifacts').write_text('A file blocks the artifacts directory')
    result, report = run_check(application)
    assert result.returncode == 1
    assert next(c for c in report['checks'] if c['name'] == 'artifacts')['status'] == 'FAIL'
    assert next(c for c in report['checks'] if c['name'] == 'mcp.stdio')['status'] == 'SKIP'


def test_preflight_bounds_a_server_that_never_speaks_mcp(application):
    application.write_text(application.read_text().replace('    app.run()', '    import time\n    time.sleep(60)'))
    result, report = run_check(application, '--timeout', '2')
    assert result.returncode == 1
    assert next(c for c in report['checks'] if c['name'] == 'mcp.stdio')['status'] == 'FAIL'


def test_preflight_bounds_blocking_imports(application):
    application.write_text('import time\ntime.sleep(60)\n')
    result, report = run_check(application, '--timeout', '1')
    assert result.returncode == 1
    assert next(c for c in report['checks'] if c['name'] == 'inspection')['status'] == 'FAIL'


def test_preflight_catches_imports_that_depend_on_server_working_directory(application):
    application.with_name('server-only.txt').write_text('available only at startup')
    office = application.with_name('office.py')
    office.write_text('from pathlib import Path\nPath("server-only.txt").read_text()\n' + office.read_text())
    result, report = run_check(application)
    assert result.returncode == 1
    assert next(c for c in report['checks'] if c['name'] == 'application')['status'] == 'PASS'
    assert next(c for c in report['checks'] if c['name'] == 'function.import')['status'] == 'FAIL'
