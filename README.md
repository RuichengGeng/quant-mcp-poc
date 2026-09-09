# Quant MCP POC

Give Claude Desktop a task; let a coding agent write, run, and debug Python that uses your existing deterministic functions.

MCP is the entry point for the whole task. Your functions stay in ordinary Python modules: the agent receives their signatures and docstrings, then imports them in generated code. There is no need to expose every function as an MCP tool or HTTP API.

This demo includes a small option-pricing library and three agent adapters: **smolagents**, Pi RPC, and a limited local demo that needs no model credentials. The setup below uses smolagents with DeepSeek.

## How it works

```text
Claude Desktop → MCP task tool → worker process → coding agent
                                                    ↓
                                        write complete analysis.py
                                                    ↓
                                        import your Python functions
                                                    ↓
                                        execute → validate → repair if needed
                                                    ↓
                                        result.json + saved artifacts
```

Each smolagents code action is a complete script. The executor saves it as `analysis.py`, runs it with the project's Python interpreter, and checks the result schema and artifact paths. Failures and tracebacks go back to the agent for repair. A successful script ends the task and is not executed again by the runner.

MCP tasks run automatically, without terminal review or MCP elicitation. Generated code runs as ordinary local Python; the worker process is **not a security sandbox**. Result validation checks the output contract, not the numerical correctness of every calculation.

## Quick start: smolagents + Claude Desktop

You need Python 3.11+, `uv`, and a DeepSeek API key.

From the project directory:

```bash
uv sync --extra smolagents-demo
cp .env.example .env
```

Edit these values in `.env` (the example file initially selects Pi):

```dotenv
QUANT_MCP_AGENT=smolagents
DEEPSEEK_API_KEY=your-api-key
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=disabled
```

The server loads the project's `.env`; existing process environment variables take priority. Keep credentials in `.env`, which is ignored by Git.

In Claude Desktop's MCP configuration, add the server to `mcpServers`. Replace both absolute paths below; `command -v uv` shows your `uv` path.

```json
{
  "mcpServers": {
    "quant-mcp-poc": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run",
        "--project", "/absolute/path/to/quant-mcp-poc",
        "--extra", "smolagents-demo",
        "quant-mcp"
      ]
    }
  }
}
```

Use **Developer → Reload MCP Configuration**, or fully quit and reopen Claude Desktop, after changing the server configuration or `.env`.

Try asking Claude:

> Use run_quant_coding_task to price a European put with spot 100, strike 110, time to expiry 0.25 years, annual volatility 25%, annual risk-free rate 4%, and dividend yield 0%. Use the registered functions and report the premium and Greeks, including their units.

The MCP tools are:

| Tool | Purpose |
| --- | --- |
| `run_quant_coding_task(prompt, timeout_seconds=300)` | Write, execute, and repair a task. |
| `list_artifacts()` | List recent tasks. |
| `read_artifact(task_id, filename)` | Read a saved text artifact or get a binary artifact's path. |
| `analyze_option_portfolio(prompt, timeout_seconds=120)` | Backward-compatible task entry point. Prefer `run_quant_coding_task`. |

## Add functions for smolagents to use

### 1. Write a normal Python function

For example, create `src/quant_mcp/analytics.py`:

```python
def position_pnl(
    entry_price: float,
    current_price: float,
    quantity: float,
    multiplier: float = 1.0,
) -> float:
    """Return unrealized P&L in the same currency as the prices.

    entry_price and current_price are prices per unit.
    quantity is signed: positive for long positions, negative for short.
    multiplier is units per contract; use 100 for a 100-share contract.
    Fees and financing costs are excluded. Returns a single float.
    """
    return (current_price - entry_price) * quantity * multiplier
```

Use clear type hints and docstrings. Explain units, sign conventions, defaults, and the exact return shape. For dictionary results, document the keys. For rates and volatility, state whether inputs are decimals such as `0.25` or percentages such as `25`. These details are passed to the agent and help it call your code correctly.

### 2. Register the module

Add this line to `.env`:

```dotenv
QUANT_MCP_LIBRARY_MODULES=quant_mcp.pricing,quant_mcp.analytics
```

This setting **replaces** the default module list, so include `quant_mcp.pricing` if you want to keep the pricing functions available. Reload the MCP configuration after changing it.

Discovery finds public Python functions defined in each registered module or its submodules. A registered package must expose those functions from its `__init__.py`. For example, a new function in `pricing/primitives.py` must also be imported in `pricing/__init__.py` to appear under the registered `quant_mcp.pricing` package. Private names beginning with `_`, classes, and functions imported from unrelated modules are excluded.

No tool decorator, MCP schema change, or agent adapter change is needed. The generated script can simply use:

```python
from quant_mcp.analytics import position_pnl

pnl = position_pnl(entry_price=2.0, current_price=3.5, quantity=2, multiplier=100)
# 300.0
```

### 3. Check discovery and run a task

From the project directory, inspect the same catalog the agent receives:

```bash
uv run --extra smolagents-demo python - <<'PY'
from quant_mcp.config import load_env_file
from quant_mcp.library_catalog import discover_library_catalog

load_env_file()
print(discover_library_catalog().render_for_prompt())
PY
```

Then ask Claude to use `run_quant_coding_task`:

> Calculate unrealized P&L for two long contracts bought at 2.00 and now worth 3.50, with 100 units per contract. Use quant_mcp.analytics.position_pnl and save the result.

Check the task's `analysis.py` to see the imports and calls. `library_catalog.json` and `agent_instruction.md` show exactly what was made available to the agent.

### Use an existing library

You can keep functions in a separate Python package. Install it into this project's environment, then register its module:

```bash
uv add --editable /absolute/path/to/my-quant-library
```

```dotenv
QUANT_MCP_LIBRARY_MODULES=quant_mcp.pricing,my_quant_library.analytics
```

The library needs to be importable by the server's Python interpreter. Register the module that defines the public functions, or a package that re-exports its own functions.

The bundled `quant_mcp.pricing` exports `price_option` and `calc_greeks` for date-based inputs, plus `price_option_t` and `calc_greeks_t` for time to expiry in years. Its docstrings specify decimal rates and volatility, per-unit prices, and Greek units.

## Run from the CLI

The CLI calls the shared task runner directly. With `QUANT_MCP_AGENT=smolagents` in `.env`, this runs without review:

```bash
uv run --extra smolagents-demo quant-cli run --json \
  "Price a European put: spot 100, strike 110, expiry in 0.25 years, vol 25%, rate 4%, dividend yield 0%."
```

An explicit `--agent smolagents` enables interactive terminal review of generated scripts. This is separate from the automatic MCP path. The old `review_mode` tool argument is removed, and `QUANT_MCP_REVIEW_MODE` is ignored.

For a limited, credential-free option demo:

```bash
uv run quant-cli run --agent local \
  "I am long an AAPL 180/200 call spread expiring Dec 2026. Spot 185, vol 24%, rate 4%."
```

Inspect previous tasks:

```bash
uv run quant-cli list
uv run quant-cli read YOUR_TASK_ID result.json
```

The separate `examples/smolagents_review_demo.py` is an optional experiment using typed tool wrappers and interactive review. It is not the MCP adapter described above.

## Configuration and troubleshooting

| Setting | Default | Purpose |
| --- | --- | --- |
| `QUANT_MCP_AGENT` | `local` when unset | Choose `smolagents`, `pi`, or the limited `local` demo. |
| `QUANT_MCP_LIBRARY_MODULES` | `quant_mcp.pricing` | Comma-separated modules to discover. |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Model used by the smolagents adapter. |
| `DEEPSEEK_API_BASE` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `DEEPSEEK_THINKING` | `disabled` | Set to `enabled` for tasks that benefit from longer reasoning. |
| `SMOLAGENTS_MODEL_TIMEOUT_SECONDS` | `120` | Timeout per model request, capped by the supplied task timeout. |
| `SMOLAGENTS_MAX_STEPS` | `8` | Code-step budget, including repairs. |

The smolagents adapter reads `DEEPSEEK_API_KEY`, falling back to `PI_API_KEY`. MCP's `timeout_seconds` bounds the entire task, including model calls, execution, and repairs. A timeout terminates the worker and its child processes and returns a failed result.

Each task saves its work under `artifacts/{task_id}/`:

| File | What to inspect |
| --- | --- |
| `prompt.txt`, `agent_instruction.md` | Original request and agent instructions. |
| `library_catalog.json` | Discovered function signatures and docstrings. |
| `analysis.py`, `run.log` | Generated code and execution output. |
| `result.json` | Structured status, summary, metrics, assumptions, and artifacts. |
| `smolagents_result.json`, `code_review.md` | Agent steps and execution approval records. |
| `attempts/` | Failed scripts and logs retained for debugging. |

Task-specific CSV, HTML, Excel, or other outputs are saved alongside these files when requested. Agent console output is isolated in `artifacts/worker_logs/` so it cannot corrupt MCP's stdout protocol. MCP responses include the `worker_log` path.

If Claude appears stuck, inspect that worker log and the latest task folder. If a function is missing, check the catalog, module registration, and package exports. Reload MCP configuration after server updates so Claude uses the current process and tool schema.

## Alternative: Pi RPC

To use an installed Pi CLI, set these values in `.env`:

```dotenv
QUANT_MCP_AGENT=pi
PI_EXECUTABLE=pi
PI_PROVIDER=deepseek
PI_MODEL=v4flash
PI_THINKING=medium
PI_API_KEY=
PI_SESSION_DIR=
```

Leave `PI_API_KEY` blank if Pi already has authentication configured. The adapter starts Pi in RPC mode, sends the task over stdin, and saves events in `pi_rpc_events.jsonl`. Once Pi writes a runnable deliverable or settles, the shared runner executes and validates the result. The Pi/local path allows two repair attempts by default; smolagents uses its own code-step budget.

## Development

```bash
uv sync --extra smolagents-demo
uv run --extra smolagents-demo pytest
```
