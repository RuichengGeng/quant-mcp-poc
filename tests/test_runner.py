from pathlib import Path

import yaml

from quant_mcp.agent import CodingAgent, LocalDemoAgent
from quant_mcp.agent.base import AgentResult
from quant_mcp.runner import _agent_instruction, run_quant_task


class FailingAgent(CodingAgent):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        return AgentResult(status="failed", message="simulated failure")


class ExplodingAgent(CodingAgent):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        raise RuntimeError("simulated startup crash")


class HandRolledPricerAgent(CodingAgent):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        (workspace_dir / "analysis.py").write_text(
            "\n".join(
                [
                    "import json",
                    "import math",
                    "from pathlib import Path",
                    "",
                    "def price_call():",
                    "    return math.exp(-0.04 * 0.25) * 1.23",
                    "",
                    "Path('result.json').write_text(json.dumps({",
                    "    'status': 'success',",
                    "    'task_type': 'option_pricing',",
                    "    'summary': 'priced with local formula',",
                    "    'metrics': {'premium': price_call()},",
                    "    'assumptions': [],",
                    "    'artifacts': [],",
                    "}))",
                ]
            ),
            encoding="utf-8",
        )
        return AgentResult(status="success", message="wrote handwritten pricing script")


class LibraryPricerAgent(CodingAgent):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        (workspace_dir / "analysis.py").write_text(
            "\n".join(
                [
                    "import json",
                    "from pathlib import Path",
                    "from quant_mcp.pricing import calc_greeks_t, price_option_t",
                    "",
                    "premium = price_option_t('call', 100, 110, 0.25, 0.25, 0.04)",
                    "greeks = calc_greeks_t('call', 100, 110, 0.25, 0.25, 0.04)",
                    "Path('result.json').write_text(json.dumps({",
                    "    'status': 'success',",
                    "    'task_type': 'option_pricing',",
                    "    'summary': 'priced with quant_mcp.pricing',",
                    "    'metrics': {'premium': premium, **greeks},",
                    "    'assumptions': [],",
                    "    'artifacts': [],",
                    "}))",
                ]
            ),
            encoding="utf-8",
        )
        return AgentResult(status="success", message="wrote library pricing script")


class DottedImportPricerAgent(CodingAgent):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        (workspace_dir / "analysis.py").write_text(
            "\n".join(
                [
                    "import json",
                    "from pathlib import Path",
                    "import quant_mcp.pricing",
                    "",
                    "premium = quant_mcp.pricing.price_option_t('call', 100, 110, 0.25, 0.25, 0.04)",
                    "Path('result.json').write_text(json.dumps({",
                    "    'status': 'success',",
                    "    'task_type': 'option_pricing',",
                    "    'summary': 'priced with quant_mcp.pricing',",
                    "    'metrics': {'premium': premium},",
                    "    'assumptions': [],",
                    "    'artifacts': [],",
                    "}))",
                ]
            ),
            encoding="utf-8",
        )
        return AgentResult(status="success", message="wrote dotted import pricing script")


class MixedLibraryAndHandRolledPricerAgent(CodingAgent):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        (workspace_dir / "analysis.py").write_text(
            "\n".join(
                [
                    "import json",
                    "from pathlib import Path",
                    "from statistics import NormalDist",
                    "from quant_mcp.pricing import price_option_t",
                    "",
                    "def bs_call_price():",
                    "    return NormalDist().cdf(0.0)",
                    "",
                    "premium = price_option_t('call', 100, 110, 0.25, 0.25, 0.04)",
                    "Path('result.json').write_text(json.dumps({",
                    "    'status': 'success',",
                    "    'task_type': 'option_pricing',",
                    "    'summary': 'priced with mixed logic',",
                    "    'metrics': {'premium': premium, 'extra': bs_call_price()},",
                    "    'assumptions': [],",
                    "    'artifacts': [],",
                    "}))",
                ]
            ),
            encoding="utf-8",
        )
        return AgentResult(status="success", message="wrote mixed pricing script")


class RepairingAgent(CodingAgent):
    def __init__(self) -> None:
        self.calls = 0

    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            source = """from quant_mcp.pricing import price_option_t
price_option_t('call', 100, 110, 0.25, 0.25, 0.04)
raise RuntimeError('first attempt')
"""
        else:
            source = """import json
from pathlib import Path
from quant_mcp.pricing import price_option_t
premium = price_option_t('call', 100, 110, 0.25, 0.25, 0.04)
Path('result.json').write_text(json.dumps({
    'status': 'success', 'task_type': 'option_pricing',
    'summary': 'repaired', 'metrics': {'premium': premium},
    'assumptions': [], 'artifacts': []
}))
"""
        (workspace_dir / "analysis.py").write_text(source, encoding="utf-8")
        return AgentResult(status="success", message=f"attempt {self.calls}")


def test_runner_creates_required_artifacts() -> None:
    task = run_quant_task(
        "I am long AAPL 180/200 call spread expiring Dec 2026. Spot 185, vol 24%, rate 4%.",
        agent=LocalDemoAgent(),
        timeout_seconds=60,
    )

    assert task.result["status"] == "success"
    for filename in [
        "portfolio.yaml",
        "prompt.txt",
        "agent_instruction.md",
        "library_catalog.json",
        "analysis.py",
        "run.log",
        "result.json",
        "report.html",
        "positions.csv",
        "metrics.csv",
        "scenarios.csv",
        "scenarios.xlsx",
    ]:
        assert (Path(task.task_dir) / filename).exists()


def test_runner_does_not_price_options_for_greeting() -> None:
    task = run_quant_task("how are you", agent=LocalDemoAgent(), timeout_seconds=60)

    assert task.result["status"] == "success"
    assert task.result["task_type"] == "no_action"
    assert not (Path(task.task_dir) / "analysis.py").exists()


def test_agent_instruction_warns_not_to_pass_numeric_t_as_expiry(tmp_path) -> None:
    instruction = _agent_instruction("price option with T=0.25", tmp_path)

    assert "Use price_option_t/calc_greeks_t" in instruction
    assert "Do not reimplement Black-Scholes formulas" in instruction


def test_runner_rejects_option_pricing_without_pricing_api() -> None:
    task = run_quant_task(
        "price a call option with S=100, K=110, T=0.25, vol=25%, rate=4%",
        agent=HandRolledPricerAgent(),
        timeout_seconds=60,
    )

    assert task.result["status"] == "failed"
    assert "did not call a registered deterministic library API" in task.result["summary"]


def test_runner_accepts_option_pricing_with_pricing_api() -> None:
    task = run_quant_task(
        "price a call option with S=100, K=110, T=0.25, vol=25%, rate=4%",
        agent=LibraryPricerAgent(),
        timeout_seconds=60,
    )

    assert task.result["status"] == "success"
    assert task.result["metrics"]["premium"] > 0


def test_runner_accepts_dotted_pricing_import() -> None:
    task = run_quant_task(
        "price a call option with S=100, K=110, T=0.25, vol=25%, rate=4%",
        agent=DottedImportPricerAgent(),
        timeout_seconds=60,
    )

    assert task.result["status"] == "success"


def test_runner_repairs_script_in_same_task_folder() -> None:
    agent = RepairingAgent()

    task = run_quant_task(
        "price a call option with S=100, K=110, T=0.25, vol=25%, rate=4%",
        agent=agent,
        timeout_seconds=60,
        repair_attempts=1,
    )

    assert task.result["status"] == "success"
    assert agent.calls == 2
    assert (Path(task.task_dir) / "attempts" / "attempt_01" / "analysis.py").exists()
    assert (Path(task.task_dir) / "attempts" / "attempt_01" / "failure.txt").read_text().strip()
    assert (Path(task.task_dir) / "repair_instruction_01.md").exists()


def test_runner_rejects_mixed_library_and_handwritten_pricer() -> None:
    task = run_quant_task(
        "price a call option with S=100, K=110, T=0.25, vol=25%, rate=4%",
        agent=MixedLibraryAndHandRolledPricerAgent(),
        timeout_seconds=60,
    )

    assert task.result["status"] == "failed"
    assert "reimplement option pricing" in task.result["summary"]


def test_runner_option_prompt_accepts_underlying_and_relative_expiry() -> None:
    task = run_quant_task(
        "i would like to do a call spread: underlying 100, long call spread expirying in 3 months "
        "with strike 110/120, how much is premium and greeks profile",
        agent=LocalDemoAgent(),
        timeout_seconds=60,
    )

    portfolio = yaml.safe_load((Path(task.task_dir) / "portfolio.yaml").read_text(encoding="utf-8"))

    assert portfolio["market"]["spot"]["DEMO"] == 100.0
    assert portfolio["positions"][0]["strike"] == 110.0
    assert portfolio["positions"][0]["side"] == "long"
    assert portfolio["positions"][1]["strike"] == 120.0
    assert portfolio["positions"][1]["side"] == "short"
    assert portfolio["positions"][0]["expiry"] != "2027-09-03"


def test_runner_option_prompt_accepts_structured_call_spread_fields() -> None:
    task = run_quant_task(
        "\n".join(
            [
                "Price a long European call spread using Black-Scholes.",
                "Parameters:",
                "- Spot S = 100",
                "- Long call strike K1 = 110, short call strike K2 = 120",
                "- Time to expiry T = 0.25 years (3 months)",
                "- Risk-free rate r = 4.0% continuously compounded",
                "- Dividend yield q = 0.0",
                "- Implied volatility = 25% flat for both legs",
            ]
        ),
        agent=LocalDemoAgent(),
        timeout_seconds=60,
    )

    portfolio = yaml.safe_load((Path(task.task_dir) / "portfolio.yaml").read_text(encoding="utf-8"))

    assert portfolio["market"]["spot"]["DEMO"] == 100.0
    assert portfolio["market"]["volatility"]["DEMO"] == 0.25
    assert portfolio["positions"][0]["strike"] == 110.0
    assert portfolio["positions"][0]["side"] == "long"
    assert portfolio["positions"][1]["strike"] == 120.0
    assert portfolio["positions"][1]["side"] == "short"
    assert portfolio["positions"][0]["expiry"] != "2027-09-04"


def test_runner_reports_env_selected_agent_failure_without_fallback(monkeypatch) -> None:
    monkeypatch.setattr("quant_mcp.runner.build_agent", lambda: FailingAgent())
    monkeypatch.delenv("QUANT_MCP_ALLOW_FALLBACK", raising=False)

    task = run_quant_task(
        "long call spread with spot 100, strikes 110/120, vol 20%, rate 4%",
        timeout_seconds=60,
    )

    assert task.result["status"] == "failed"
    assert "simulated failure" in task.result["summary"]
    assert not (Path(task.task_dir) / "analysis.py").exists()


def test_runner_reports_agent_exceptions_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr("quant_mcp.runner.build_agent", lambda: ExplodingAgent())
    monkeypatch.delenv("QUANT_MCP_ALLOW_FALLBACK", raising=False)

    task = run_quant_task("long call spread with spot 100, strikes 110/120", timeout_seconds=60)

    assert task.result["status"] == "failed"
    assert "simulated startup crash" in task.result["summary"]
    assert (Path(task.task_dir) / "run.log").exists()


def test_runner_fallback_is_opt_in(monkeypatch) -> None:
    monkeypatch.setattr("quant_mcp.runner.build_agent", lambda: FailingAgent())
    monkeypatch.setenv("QUANT_MCP_ALLOW_FALLBACK", "1")

    task = run_quant_task(
        "long call spread with spot 100, strikes 110/120, vol 20%, rate 4%",
        timeout_seconds=60,
    )

    assert task.result["status"] == "success"
    assert (Path(task.task_dir) / "analysis.py").exists()
