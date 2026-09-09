from __future__ import annotations

import importlib
import inspect
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_LIBRARY_MODULES = ("quant_mcp.pricing",)


@dataclass(frozen=True)
class LibraryFunction:
    module: str
    name: str
    signature: str
    description: str


@dataclass(frozen=True)
class LibraryCatalog:
    modules: tuple[str, ...]
    functions: tuple[LibraryFunction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "modules": list(self.modules),
            "functions": [asdict(function) for function in self.functions],
        }

    def render_for_prompt(self) -> str:
        lines = ["REGISTERED DETERMINISTIC LIBRARY APIs:"]
        if not self.functions:
            lines.append("- No library APIs were discovered.")
        for function in self.functions:
            description = f" - {function.description}" if function.description else ""
            lines.append(f"- {function.module}.{function.name}{function.signature}{description}")
        return "\n".join(lines)

    def function_names_by_module(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for function in self.functions:
            result.setdefault(function.module, set()).add(function.name)
        return result


def discover_library_catalog(modules: tuple[str, ...] | None = None) -> LibraryCatalog:
    configured = modules or _configured_modules()
    functions: list[LibraryFunction] = []
    for module_name in configured:
        module = importlib.import_module(module_name)
        for name, value in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_") or not _belongs_to_registered_module(value, module_name):
                continue
            functions.append(
                LibraryFunction(
                    module=module_name,
                    name=name,
                    signature=_render_signature(value),
                    description=inspect.getdoc(value) or "",
                )
            )
    return LibraryCatalog(
        modules=tuple(configured),
        functions=tuple(sorted(functions, key=lambda item: (item.module, item.name))),
    )


def write_catalog(catalog: LibraryCatalog, path: Path) -> None:
    path.write_text(json.dumps(catalog.to_dict(), indent=2) + "\n", encoding="utf-8")


def _render_signature(function: Any) -> str:
    # Resolve postponed annotations such as OptionType into Literal['call', 'put'].
    # Otherwise the model may try to import a type alias that is not exported.
    try:
        return str(inspect.signature(function, eval_str=True))
    except (NameError, TypeError, ValueError):
        return str(inspect.signature(function))


def _configured_modules() -> tuple[str, ...]:
    raw = os.getenv("QUANT_MCP_LIBRARY_MODULES", "")
    if not raw.strip():
        return DEFAULT_LIBRARY_MODULES
    return tuple(module.strip() for module in raw.split(",") if module.strip())


def _first_doc_line(docstring: str | None) -> str:
    if not docstring:
        return ""
    return next((line.strip() for line in docstring.splitlines() if line.strip()), "")


def _belongs_to_registered_module(value: Any, module_name: str) -> bool:
    implementation_module = getattr(value, "__module__", "")
    return implementation_module == module_name or implementation_module.startswith(f"{module_name}.")
