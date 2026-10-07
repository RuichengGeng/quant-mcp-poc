# Mini Agent Study Guide — from ReAct to a self-written coding agent

Goal: understand the **agent runtime minimal loop** well enough to write ~300–800 lines
of Python that can inspect a repo, edit files, run tests, and finish — with **no agent
framework**. Then read mature agents (smolagents, Pi, Claude Code, Codex, OpenHands)
and see them as the same skeleton plus engineering.

Scope discipline: this guide deliberately excludes multi-agent, long-term memory, vector
DBs, sandbox internals, PTY/terminal emulation, and LangGraph. They come *after* the loop.

---

## 0. The one diagram everything collapses into

```
        ┌─────────────────────────────────────────────┐
        │                 AGENT RUNTIME               │
        │                                             │
 task ─▶│  state/history ─▶ build context ─▶ LLM call │
        │        ▲                                │   │
        │        │                                ▼   │
        │   append observation            parse output │
        │        │                                │   │
        │        │                          ┌─────┴─────┐
        │        │                       tool call?   final?
        │        │                          │            │
        │        └──── executor ◀───────────┘            ▼
        │                 │                          answer/done
        │                 ▼                              │
        │        environment (fs / shell / API / MCP) ◀──┘
        └─────────────────────────────────────────────┘
```

Every agent you have used is this. Differences live in **context, execution, state,
orchestration** — not in the core shape.

Five layers to keep straight:

```
LLM              reasoning / decision
Agent Loop       who decides when to continue or stop
Context / State  what the model sees, what the runtime remembers
Tools / Skills   how capability is exposed to the model
Environment      where effects actually happen (files, shell, APIs)
```

---

## 1. ReAct, precisely

**Paper:** Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models*,
arXiv:2210.03629 (2022). https://arxiv.org/abs/2210.03629

### The claim
Interleave **reasoning** and **acting** in one trajectory instead of doing either alone.

- **Reason-only (chain-of-thought):** reasons well but is ungrounded → hallucinated facts,
  no ability to affect the world.
- **Act-only:** can call tools but has no explicit planning/reflection → dumb action sequences.
- **ReAct:** reasoning picks better actions; actions ground the reasoning with real evidence.

### The loop

```
Question / State
  → Thought_1   (natural-language reasoning: plan, decompose, reflect)
  → Action_1    (an explicit command: search[...], lookup[...], or env action)
  → Observation_1 (the result returned by the environment)
  → Thought_2
  → Action_2
  → Observation_2
  → ...
  → Final Answer
```

Pseudocode:

```
context = [question]
while not done:
    thought = LLM_gen("Thought:", context)
    action  = LLM_gen("Action:", context)
    obs     = environment.execute(action)      # external, not the LLM
    context += [thought, action, obs]
    done    = model_emitted_final_answer
```

### Critical detail people miss
ReAct as published is a **prompting format**, not an API feature. The model emits literal
text like `Thought: ... Action: search[apple]`, and a **parser written by you** turns that
text into a call. The environment executes it and you paste the result back. Everything
is string-to-string.

### What ReAct is NOT
- It is not "the only agent architecture." It is one loop shape.
- It is not a training method (it's prompting).
- It does not define memory, sandboxing, permissions, or multi-agent — those are later layers.

### The bridge to today
Modern agents keep the ReAct *loop* but replace the *text format* with **native function
calling** (structured tool schemas enforced by the API). The thought is often implicit or
in a separate "reasoning" channel. So:

```
ReAct (2022):  Thought/Action/Observation as TEXT, parsed by regex
Today:         same loop, but tool calls are STRUCTURED JSON via the API
Code agents:   same loop, but the "action" is a block of executable code
```

This is why your intuition that smolagents / OpenAI Agents SDK / Claude Code "feel the
same" is correct — they are the same loop with a different action surface.

---

## 2. Vocabulary you must be able to define (no hand-waving)

| Term | Definition | The subtlety that bites people |
|---|---|---|
| **Agent loop** | The `while` that repeatedly calls the LLM, executes actions, appends results. | The *runtime*, not the model, owns this loop. The model is stateless per call. |
| **Turn / step** | One iteration: one LLM call + zero-or-more tool executions. | Steps ≠ LLM calls; one step can batch several parallel tool calls. |
| **Tool** | A callable capability exposed to the model with a name, description, and JSON-schema parameters. | The description is *prompt engineering*; bad descriptions cause bad tool choice. |
| **Tool call** | The model's structured request to invoke a tool with arguments. | It is a *request*, not an execution. The model never runs anything. |
| **Executor** | The runtime component that parses the tool call and actually runs the code. | This is your trust boundary — where safety/permission checks belong. |
| **Observation** | The result of execution that gets appended back into context. | Errors are observations too. Never crash the loop on a tool error. |
| **State** | Everything the runtime tracks: messages, step count, cwd, budget, artifacts. | State ≠ context. State can be huge; context is what you send this call. |
| **Context** | The exact token sequence sent to the model this turn. | Context is a *projection* of state, often compressed/truncated. |
| **Termination** | Why the loop stops: model said done, step cap hit, error, timeout, budget. | You must define it explicitly or you get infinite loops. |
| **Harness / runtime** | The full program around the model: loop + context + executor + environment. | "Harness" is just the plumbing that turns an LLM into an agent. |

---

## 3. The minimal runtime, component by component

Six components. Build them in this order.

### 3.1 Model client
- One function: `call_llm(messages, tools) -> response`.
- Responsibilities: build the request, pass tool schemas, return a normalized object.
- Failure modes: rate limits, malformed args, context overflow. Handle here or above.

### 3.2 Tool registry
- A dict of `name -> {schema, callable}`.
- The schema is what the model sees; the callable is what you run.
- Keep the two in sync. Hand-written JSON schema is fine for a first build.
- **Do not abstract yet.** No `BaseTool`, no `ToolRegistryFactory`. A dict of functions.

### 3.3 Output parser
- Turns the raw model response into one of: `{tool_calls: [...]}` or `{final: text}`.
- With native function calling this is mostly free (the API returns structured calls).
- With text-ReAct you write the parser and it is the fragile part.

### 3.4 Executor
- Runs a tool call, catches exceptions, returns an observation string.
- Rules:
  - Never let a tool exception kill the loop.
  - Convert failures to *informative* text: `Tool failed: FileNotFoundError: a.py`.
  - Enforce timeouts and cwd bounds here.
- This is where permissions/sandboxing will later live.

### 3.5 State / history
- An append-only list of messages (or a small class wrapping it).
- The one invariant that matters: **every assistant tool call must be followed by a
  matching tool result**, or the next API call is invalid.
- Later you will add: step count, token budget, cwd, artifacts, summaries.

### 3.6 Loop controller
- `while step < max_steps:` → build context → call → branch on tool/final → execute → append.
- Owns termination. Owns the step counter. Owns the budget.

### The minimal loop (shape only — you write the real thing)

```python
def run_agent(task: str) -> str:
    messages = [system_prompt(), {"role": "user", "content": task}]

    for step in range(MAX_STEPS):
        response = call_llm(messages, tools=TOOL_SCHEMAS)

        if not response.tool_calls:
            return response.content                      # done

        messages.append(response.as_assistant_message())  # must precede results

        for call in response.tool_calls:
            try:
                result = execute_tool(call)
            except Exception as e:
                result = f"Tool failed: {type(e).__name__}: {e}"
            messages.append(tool_message(call.id, result))  # matching tool_call_id

    return "Stopped: max steps reached"
```

---

## 4. The message protocol (get this exactly right)

This is the part that silently breaks people. The model is stateless; the **message list
is the agent's only memory**.

### OpenAI Chat Completions style

```json
[
  {"role": "system", "content": "..."},
  {"role": "user", "content": "Fix the failing tests."},

  {"role": "assistant", "content": null,
   "tool_calls": [
     {"id": "call_1", "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\": \"calc.py\"}"}}
   ]},

  {"role": "tool", "tool_call_id": "call_1", "content": "def divide(a,b): return a*b"},

  {"role": "assistant", "content": "The bug is in divide()."}
]
```

Rules:
- `tool_calls[i].id` must equal the later `tool.tool_call_id`.
- The assistant message carrying `tool_calls` **must** be followed by one tool message
  per call before the next assistant turn.
- `arguments` is a JSON **string** (not an object) in this API.

### OpenAI Responses API
Same idea, different names: `function_call` items and `function_call_output` with
`call_id`. Do not mix the two APIs' shapes.

### Anthropic Messages API
Tool calls are content blocks, and you signal the loop via `stop_reason`:
- Assistant content: `[{"type":"text",...}, {"type":"tool_use","id":"toolu_1","name":"read_file","input":{"path":"calc.py"}}]`
- `stop_reason == "tool_use"` → you must execute and return results.
- Your reply: user message with `[{"type":"tool_result","tool_use_id":"toolu_1","content":"..."}]`

Note the vocabulary shift: **content blocks**, `tool_use`, `tool_result`, `stop_reason`.
If you build an abstraction over providers, *this* is the layer to abstract — not the loop.

### Tool schema shape (the model-facing contract)

```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "Read a UTF-8 text file inside the workspace.",
    "parameters": {
      "type": "object",
      "properties": {"path": {"type": "string", "description": "Workspace-relative path"}},
      "required": ["path"]
    }
  }
}
```

The `description` is how the model decides *when* to use it. Treat it as prompt text.

---

## 5. The coding ladder (build it yourself, in this order)

Each rung has an **acceptance test**. Do not move on until it passes. Each has a **trap**.

### Rung 0 — Bare model call
Wrap one chat call with no tools. Get a string back.
- **Accept when:** a script prints a model answer.
- **Trap:** pulling in a framework "to be safe." Don't.

### Rung 1 — First agent: 2–3 trivial tools
`calculator`, `get_current_time`, `finish(answer)`.
- **Accept when:** "What is 1234 * 567?" makes the model call `calculator` and then return.
- **Trap:** designing `BaseTool` / abstract classes. Not yet.

### Rung 2 — Explicit termination
Add `MAX_STEPS` and an explicit `finish` tool (or treat "no tool calls" as done).
- **Accept when:** a task that loops forever instead stops cleanly at step N.
- **Trap:** relying on the model to stop. It won't, reliably.

### Rung 3 — File tools + workspace jail
`read_file`, `write_file`, `list_files`, `search_files`, all confined to `workspace/`.
- **Accept when:** the agent fixes a seeded bug in `workspace/calculator.py` by reading
  and rewriting the file.
- **Trap:** path traversal (`../../etc/passwd`). Resolve and assert the path is inside root.

### Rung 4 — Shell executor
`run_command` with `cwd=workspace` and a hard timeout.
- **Accept when:** the agent runs `pytest`, reads the failure, edits, re-runs, and passes.
- **Trap:** unbounded output flooding context. Truncate to N lines before appending.
- **This is the Aha moment:** SWE agents are not "better coders." They have
  **LLM + environment feedback loop.**

### Rung 5 — Error-as-observation
Wrap every tool call in try/except and feed failures back as text.
- **Accept when:** you delete a file, the agent gets `FileNotFoundError`, and it recovers
  by listing the directory instead of crashing.
- **Trap:** raising out of the loop. The loop must survive any single tool failure.

### Rung 6 — Context management (first pass)
- Truncate giant tool outputs (head/tail + "… N lines omitted").
- Cap history by keeping system + task + last K steps.
- **Accept when:** a 30-step run doesn't blow the context window.
- **Trap:** naive "delete old messages" that severs a tool call from its result →
  invalid API request. Summarize whole *pairs*, or use the provider's tool-result trimming.

### Rung 7 — Persistence & observability
- Log every step as JSON: step, tool, args, observation length, tokens.
- **Accept when:** you can replay a run from the log and explain every decision.
- **Trap:** not logging. You cannot debug an agent you cannot see.

### Rung 8 — The benchmark task
Seed a tiny repo with a deliberately wrong function and a failing test:

```python
def divide(a, b):
    return a * b   # bug
```

Prompt: `Fix the failing tests.` Do **not** hint at the location.
- **Accept when:** the agent goes list → read test → read source → edit → pytest → finish.
- **Trap:** "helping" it. The whole point is to observe the loop, not to get the fix.

---

## 6. Engineering concerns: what to add and why

Map each feature to the failure it prevents. Never add a feature without a failure.

| Concern | Failure it prevents | Minimal handling |
|---|---|---|
| `max_steps` | Infinite loop / runaway cost | Counter + hard stop |
| Timeout | A hung shell command freezes the agent | `subprocess` timeout + kill |
| Error-as-observation | One tool error kills the run | try/except → text |
| Output truncation | Context overflow from `cat` of a big file | head/tail + marker |
| Workspace jail | Agent edits your real system | Path resolution + assertion |
| Permission gate | Destructive commands (`rm -rf`) | Allowlist / confirm / deny-list |
| Token budget | Silent cost blowup | Count + stop or compress |
| Retry with backoff | Transient API/rate-limit errors | Bounded retries |
| Structured logging | Undebuggable behavior | One JSON line per step |

Later, and only with a motivating failure: context compression, sub-agents, background
tasks, sandboxing, MCP, skills/plugins, memory.

---

## 7. Self-check (answer these from memory; no notes)

1. **Agent vs a plain LLM call?**
   An agent has a runtime-owned loop with state, tools, environment feedback, and
   explicit termination. A plain call is one stateless request→response.

2. **Agent vs tool calling?**
   Tool calling is only the *mechanism* to request a function. An agent also needs the
   loop, the executor, state/history, and termination. Tool calling alone is not an agent.

3. **Who controls the loop?**
   Your runtime code — not the model. The model only emits requests and text. The
   step counter, budget, and stop conditions are yours.

4. **What does the executor do?**
   Turns a *requested* tool call into a real effect: validate args, enforce bounds and
   timeouts, run it, catch errors, return an observation string. It is the trust boundary.

5. **State vs context?**
   State is everything the runtime tracks. Context is the projection of state sent in
   this one call. Context management = deciding that projection.

6. **Why must every tool call have a matching tool result?**
   The provider validates the message sequence; an assistant `tool_calls` message must be
   immediately followed by matching `role: "tool"` messages, or the next request errors.

7. **Why is ReAct's text format mostly gone, but ReAct itself is not?**
   Native function calling replaced regex parsing of text, but the
   Thought→Action→Observation loop is exactly what modern agents still run.

8. **What makes it a *coding* agent rather than a chatbot?**
   The environment: filesystem + shell + test runner, and the loop that reacts to their
   output. Capability comes from the environment feedback loop, not the model alone.

---

## 8. Where your existing stack fits

You already run `smolagents[openai]` with a `CodeAgent`. Concretely:

| Your mini agent | smolagents |
|---|---|
| your `while` loop | `MultiStepAgent.run()` |
| your message list | `agent.memory` / step logs |
| your tool dict + schemas | `@tool`-decorated functions / `Tool` objects |
| your parser | `CodeAgent` (parses Python) vs `ToolCallingAgent` (JSON calls) |
| your `finish` tool | `final_answer` tool |
| your `MAX_STEPS` | `max_steps=` |

The **CodeAgent** difference: the "action" is a Python snippet that composes tool calls,
executed in an interpreter. Same loop, richer action surface — which is exactly your
project's `code mode` idea (model writes code that combines registered functions).
The `ToolCallingAgent` is the direct descendant of classic ReAct/tool-calling.

So the concept you have been circling — *why code mode* — is a question about the
**action surface of the loop**, not about a different architecture.

---

## 9. Reading list, in order, with a job for each

1. **ReAct paper** — arXiv:2210.03629 — https://arxiv.org/abs/2210.03629
   Read: abstract, the method figure, the algorithm box. Skip experiments.
   Job: be able to draw the Thought/Action/Observation loop unaided.

2. **OpenAI function calling guide** — https://platform.openai.com/docs/guides/function-calling
   Job: know the exact `assistant.tool_calls` / `role: "tool"` / `tool_call_id` contract.

3. **OpenAI Agents SDK docs** — https://openai.github.io/openai-agents-python/
   Read: *Agents* and *Running agents*. Job: see what a framework packages
   (`Agent` = instructions+tools; `Runner` = turn/tool/handoff/session orchestration)
   and name what you skipped.

4. **smolagents guided tour** — https://huggingface.co/docs/smolagents/index
   and the ReAct conceptual guide —
   https://huggingface.co/docs/smolagents/main/conceptual_guides/react
   Job: diff against your code. `Model → MultiStepAgent → action → executor → memory`.
   Ask: "what does each extra piece prevent?"

5. **SWE-agent paper + ACI docs** — https://arxiv.org/abs/2405.15793 and
   https://swe-agent.com/latest/background/aci/
   Job: understand that the **tool interface design** (linter-gated edits, ~100-line
   file views, succinct search, explicit "no output" feedback) moves benchmark scores
   as much as the model. This is the environment half of the loop.

6. **OpenHands agent architecture** —
   https://github.com/OpenHands/docs/blob/main/sdk/arch/agent.mdx
   Job: see the four-part decomposition — reasoning-action loop, tool orchestration,
   context management, security validation — mapped onto your 300 lines.

7. **Anthropic, Building Effective Agents** —
   https://www.anthropic.com/research/building-effective-agents
   Job: judgment. When is a plain workflow enough? When is an agent justified?
   When does multi-agent help vs add chaos? Read this last.

**Deliberately deferred:** LangGraph (workflow framework, not the agent loop — read after
you've written your own), vector DBs, sandbox internals, PTY, multi-agent orchestration.

---

## 10. Stop condition and anti-rabbit-hole rules

**Stop condition:** *I can, with no agent framework, write an agent that edits a repo and
runs its tests to green.*

When that's true, the infrastructure study is done for now. Return to the actual question:
using agents to solve problems (for you: risk / pricing / scenario / hedging workflows).

Rules that keep you out of the hole:
- No abstraction until the concrete version has a bug that the abstraction fixes.
- No feature without a failure it prevents.
- One day per rung, not one week per layer.
- When you feel the pull toward "sandbox → Docker → namespaces → cgroups," stop. That's
  reaching for ROI-zero depth. Read the docs, not the source.
- Always be able to answer, for any framework: *which of the five layers is this, and
  what did it wrap that I already wrote?*
