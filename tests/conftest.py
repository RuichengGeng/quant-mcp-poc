import pytest


@pytest.fixture(autouse=True)
def isolate_task_artifacts(tmp_path, monkeypatch):
    """Keep test runs from mixing generated fixtures with real task history."""
    monkeypatch.setattr("quant_mcp.runner.ARTIFACTS_DIR", tmp_path / "artifacts")
