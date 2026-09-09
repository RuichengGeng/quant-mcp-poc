import json
from pathlib import Path

import pytest

from quant_mcp.codebases import load_codebases, load_task_codebases, snapshot_codebases
from quant_mcp.pi_execution import execute_script


def write_map(path: Path, pricing_root: Path, oop_root: Path) -> None:
    path.write_text(
        "libraries:\n"
        "  - module: quant_mcp.pricing\n"
        f"    source: {pricing_root}\n"
        f"    examples: [{pricing_root / 'examples'}]\n"
        "    description: Pricing functions\n"
        "  - module: sample_risk\n"
        f"    source: {oop_root}\n"
        f"    examples: [{oop_root / 'examples'}]\n"
        "    description: OOP risk classes\n",
        encoding="utf-8",
    )


def test_yaml_map_resolves_multiple_codebases(tmp_path, monkeypatch):
    project = Path(__file__).parents[1]
    oop = project / "tests/fixtures/oop_repo"
    config = tmp_path / "codebases.yaml"
    write_map(config, project, oop)
    monkeypatch.setenv("QUANT_MCP_CODEBASES_FILE", str(config))

    codebases = load_codebases()

    assert codebases.modules == ("quant_mcp.pricing", "sample_risk")
    assert codebases.libraries[0].examples == (project / "examples",)
    assert oop / "src" in codebases.python_paths
    assert "OOP risk classes" in codebases.render_for_prompt()


def test_snapshot_makes_execution_independent_of_live_yaml(tmp_path, monkeypatch):
    project = Path(__file__).parents[1]
    oop = project / "tests/fixtures/oop_repo"
    config = tmp_path / "codebases.yaml"
    write_map(config, project, oop)
    monkeypatch.setenv("QUANT_MCP_CODEBASES_FILE", str(config))
    task = tmp_path / "task"
    task.mkdir()
    snapshot_codebases(task)
    config.unlink()

    loaded = load_task_codebases(task)

    assert loaded.modules == ("quant_mcp.pricing", "sample_risk")
    assert json.loads((task / "codebases.json").read_text())["libraries"][1]["module"] == "sample_risk"


def test_execution_imports_and_traces_second_yaml_library(tmp_path, monkeypatch):
    project = Path(__file__).parents[1]
    oop = project / "tests/fixtures/oop_repo"
    config = tmp_path / "codebases.yaml"
    write_map(config, project, oop)
    monkeypatch.setenv("QUANT_MCP_CODEBASES_FILE", str(config))
    task = tmp_path / "task"
    task.mkdir()
    snapshot_codebases(task)
    (task / "prompt.txt").write_text("Summarize a portfolio with the library")
    (task / "analysis.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "from sample_risk import PortfolioAnalytics, Position\n"
        "metrics = PortfolioAnalytics([Position(3, 7), Position(-2, 4)]).summarize()\n"
        "Path('result.json').write_text(json.dumps({'status':'success','summary':'ok','metrics':metrics,'assumptions':[],'artifacts':[]}))\n"
    )

    result = execute_script(task, 10)

    assert result["status"] == "success", result
    assert result["result"]["metrics"] == {"net_value": 13, "gross_value": 29}
    assert {call["symbol"] for call in result["library_calls"]} == {
        "PortfolioAnalytics.summarize", "Position.market_value"
    }


@pytest.mark.parametrize("content", ["libraries: []\n", "libraries:\n  - module: bad-name!\n    source: .\n"])
def test_invalid_yaml_map_fails_before_agent_runs(tmp_path, monkeypatch, content):
    config = tmp_path / "codebases.yaml"
    config.write_text(content)
    monkeypatch.setenv("QUANT_MCP_CODEBASES_FILE", str(config))

    with pytest.raises(ValueError, match="Invalid codebase configuration"):
        load_codebases()
