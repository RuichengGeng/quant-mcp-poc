# Examples for the repository coding agent

- `reuse_pricing.py`: a complete task script using the existing pricing functions, with decimal inputs and a structured result. Run it from a task directory using the project's Python environment. It writes `result.json` in the current directory.
- `smolagents_review_demo.py`: a standalone experiment with interactive approval and function tool wrappers. This is not the Pi/MCP execution path.

Add small, runnable workflow examples here as the library grows. State required inputs, output units, expected results, initialization, and cleanup. Pi can read these examples and the implementation files on demand; their contents are not all copied into every prompt.

For OOP libraries, demonstrate object construction and the public methods that perform the work, including required call order. Register the implementation module with `QUANT_MCP_LIBRARY_MODULES`; no function wrapper or method-by-method MCP registration is required. The executor records public Python method calls, including methods invoked through instances. Constructors alone are not evidence of domain work.

The current executor starts a fresh Python process per attempt. Examples must initialize their own objects; a persistent object/session service has not been implemented yet.
