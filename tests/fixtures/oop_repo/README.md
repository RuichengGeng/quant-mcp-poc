# OOP execution fixture

This is a small deterministic library for testing Pi's ability to read class source, learn from examples, execute existing methods, and repair reporting errors. It is not another production pricing implementation.

`src/sample_risk/__init__.py` contains the library. `examples/portfolio.py` shows normal usage. `examples/broken_report.py` is intentionally broken: execute a copy in the task workspace and repair the reporting error without changing the library. Expected values for its two positions are net 13 and gross 29.
