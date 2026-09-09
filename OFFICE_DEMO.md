# Office demo readiness — 9 September 2026

Verdict: the existing Pi + MCP framework is suitable for controlled, small demo
cases once each office library imports and its example runs in the project's
Python environment. Flue has been evaluated in discussion but is not integrated.
This review did not change runtime code or the active codebase map.

## Evidence from this review

- Full suite: **49 passed in 17.26 seconds**.
- Python 3.11 environment available; installed Pi version **0.83.0**.
- Active adapter: Pi; provider: DeepSeek; model: `deepseek-v4-flash`.
- Active YAML resolves the bundled `quant_mcp.pricing` library.
- A fresh stdio MCP session initialized and listed all four task/artifact tools.
- An offline execution test combined three YAML entries: bundled pricing, an OOP
  risk fixture copied to an external temporary repository, and a separate
  synthetic trader-data provider repository. All three produced real call traces.
- Synthetic portfolio net value was **13**, gross value **29**. The European put
  premium was **10.823125337158686** for S=100, K=110, T=0.25, vol=0.25, r=0.04,
  dividend yield=0. Put-call parity and a finite-difference delta check passed.
- An intentionally failed execution was archived, its stale result removed, and
  a manually repaired script passed. This checks executor repair behavior, not a
  fresh model-driven repair.
- A CSV report was generated. Evidence is in
  `artifacts/review_20260909/verification.json` and
  `artifacts/review_20260909/external_libraries/`.
- Existing successful Pi artifacts include pricing task
  `task_20260909_152724_017142_dfd5bfe9` and OOP task
  `task_20260909_133832_702749_e7c831a8`. These are historical evidence, not new runs.

The fresh live-model attempt returned a connection error in the restricted review
environment. Automatic approval review rejected the network-enabled retry because
project source/context could be disclosed to the external DeepSeek provider.
Consequently this review does **not** certify current model connectivity or the
office network, private libraries, database access, or provider credentials.

The external repositories used in the offline test are temporary synthetic
fixtures; their paths in the saved snapshot are review evidence, not portable
office configuration.

## Prepare the office environment

1. Use Python 3.11+ and Pi 0.83.0 for the closest match to this reviewed setup.
   From the project checkout, run `uv sync --locked` and
   `uv run --locked pytest -q`. The smolagents extra is not needed for Pi demos.
2. If moving to another computer, recreate `.venv`; do not copy the current
   virtual environment. Update the absolute `PI_EXECUTABLE` path and Claude
   Desktop's absolute `uv` and project paths.
3. Configure an office-approved model provider. Source files, examples and tool
   output the agent reads can become model context. Use synthetic/exported demo
   data and code approved for that provider during the first run.
4. Keep `QUANT_MCP_AGENT=pi` and
   `QUANT_MCP_CODEBASES_FILE=codebases.yaml` in the local `.env`.
5. Install each library's dependencies in this project's environment. A packaged
   library can be added with `uv add --editable /absolute/path/to/library-repo`.
   YAML supplies import paths but does not install packages, switch virtual
   environments, or reconcile conflicting dependency versions.

## Add the libraries

Append entries under the existing `libraries:` list. The names and paths below
are placeholders: replace them with real Python import names and existing paths.
Remove optional example paths that do not exist. Do not add a second top-level
`libraries:` key.

```yaml
libraries:
  - module: quant_mcp.pricing
    source: .
    examples: [examples]
    description: Bundled European option pricing and Greeks demo.

  - module: office_option_pricing
    source: /absolute/path/to/option-pricing-repo
    examples: [/absolute/path/to/option-pricing-repo/examples/demo.py]
    description: Option prices and Greeks; examples specify units and curve setup.

  - module: office_risk
    source: /absolute/path/to/risk-repo
    examples: [/absolute/path/to/risk-repo/examples/portfolio.py]
    description: Portfolio risk classes; examples show initialization and methods.

  - module: office_trader_data
    source: /absolute/path/to/trader-data-repo
    examples: [/absolute/path/to/trader-data-repo/examples/read_demo_data.py]
    description: Trader metrics data provider; example reads a small demo snapshot.
```

`module` is a Python import name, not a folder name or pip distribution name.
Register the package that actually defines the functions/methods, or its parent
package. A facade that re-exports implementations from an unrelated module may
need that implementation module registered as well for tracing.

`source` is the repository root. Its `src/` directory, when present, and the root
are added to Python's import path. Optional `python_paths` supplies additional
directories containing importable modules. Relative paths resolve against the
YAML file's directory. `~` expands; `$VARIABLE` placeholders are not expanded.
All listed source/example/import paths must exist before a task starts.

Check configuration and imports from the project directory after editing:

```bash
uv run --locked python - <<'PY'
import importlib
import sys
from quant_mcp.config import load_env_file
from quant_mcp.codebases import load_codebases

load_env_file()
config = load_codebases()
sys.path[0:0] = [str(path) for path in config.python_paths]
for library in config.libraries:
    module = importlib.import_module(library.module)
    print(library.module, 'OK', getattr(module, '__file__', '<namespace package>'))
PY
```

This executes normal module imports, including any import-time initialization.
Next, run one existing example per library using the same Python environment.
Examples should initialize objects, call the domain methods, state input/output
units, and close connections. Give data providers a small explicit date range,
trader identifier, and row limit. A provider that needs VPN, a database driver,
credentials or another service needs those configured separately from YAML.

Reload Claude Desktop's MCP configuration after environment/server changes.
Then ask Claude to use `run_quant_coding_task` with `timeout_seconds=300`.

## Suggested demo sequence

1. **Bundled baseline:** price the put described above, report premium and Greeks,
   and save a CSV. Expected premium is approximately 10.82312534 per share.
2. **Your pricing library:** use a known-good example and compare against its
   existing expected price and units.
3. **Your risk library:** construct a tiny portfolio, call its public calculation
   method, and compare against a known-good report.
4. **Your data provider:** fetch a small synthetic or approved read-only snapshot;
   report the actual schema, count and timestamps without inventing missing data.
5. **Composition:** fetch the small input set, pass it to existing pricing/risk
   methods, and save a combined report. Attempt this after the individual demos.

After every task, inspect `analysis.py`, `library_calls.json`, `run.log`,
`result.json`, and the reported files. Check that each library required by the
task appears in the trace and that reported values match the baseline.

## Known limits relevant to tomorrow

- **Isolation is not implemented.** Pi's direct write/edit tools are limited to
  the task workspace, but its search/read tools are not restricted to YAML roots.
  The `.env` read-name check is not a comprehensive secret boundary. Generated
  Python has ordinary host access and inherits the process environment,
  including configured credentials. YAML is a discovery map, not access control.
- **Execution state is fresh per attempt.** In-memory objects and database
  connections are not carried into the next script; keep an end-to-end demo in
  one script and initialize/close resources there.
- **Tracing is limited.** It covers public Python functions/methods on the
  execution thread. Native-only APIs or work done only in background threads may
  not satisfy it. Constructors and imports alone do not count. At least one
  registered call is required; the executor does not require every YAML library
  to be used or prove that all reported values come from traced calls.
- **Timeouts are short.** Python defaults to 60 seconds per attempt, with eight
  execution attempts; MCP defaults to 300 seconds overall. Increase
  `PI_EXECUTION_TIMEOUT_SECONDS` for a known slower example, within the overall
  task timeout. Prefer the MCP path for its whole-worker process-group deadline.
- **Pricing reimplementation detection is heuristic.** Generated scripts for
  option-pricing prompts that directly import `scipy.stats`/normal-distribution
  helpers can be rejected even for legitimate auxiliary statistics. Existing
  registered library implementations are not scanned by that check.
- **This is a demo framework.** Result-shape checks are not independent numerical
  validation. The review used synthetic data, not production risk or trader data.

For tomorrow, keep the current Pi adapter, integrate one library at a time, and
use recorded expected outputs. Flue migration and sandbox implementation can be
evaluated separately after the demo integration is proven.
