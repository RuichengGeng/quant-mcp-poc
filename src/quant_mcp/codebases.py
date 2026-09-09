"""Codebase locations for Pi, without importing or scanning user libraries."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from quant_mcp.config import PROJECT_ROOT


@dataclass(frozen=True)
class Codebase:
    module: str
    source: Path
    examples: tuple[Path, ...] = ()
    description: str = ""
    python_paths: tuple[Path, ...] = ()

    def to_dict(self) -> dict:
        return {
            "module": self.module, "source": str(self.source),
            "examples": [str(p) for p in self.examples],
            "description": self.description,
            "python_paths": [str(p) for p in self.python_paths],
        }


@dataclass(frozen=True)
class Codebases:
    libraries: tuple[Codebase, ...]

    @property
    def modules(self) -> tuple[str, ...]:
        return tuple(lib.module for lib in self.libraries)

    @property
    def primary_root(self) -> Path:
        return self.libraries[0].source

    @property
    def python_paths(self) -> tuple[Path, ...]:
        paths = []
        for lib in self.libraries:
            paths.extend(lib.python_paths)
            if (lib.source / "src").is_dir():
                paths.append(lib.source / "src")
            paths.append(lib.source)
        return tuple(dict.fromkeys(paths))

    def to_dict(self) -> dict:
        return {"libraries": [lib.to_dict() for lib in self.libraries]}

    def render_for_prompt(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False).strip()


class _UniqueKeysLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("YAML mapping keys must be strings")
            if key in keys:
                raise ValueError(f"Duplicate YAML key: {key}")
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def _path(value, base: Path, label: str, directory: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty path string")
    path = Path(value).expanduser()
    path = (base / path if not path.is_absolute() else path).resolve()
    if not (path.is_dir() if directory else path.exists()):
        raise ValueError(f"{label} does not exist or is not a {'directory' if directory else 'file/directory'}: {path}")
    return path


def _paths(value, base: Path, label: str, directory: bool) -> tuple[Path, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a path string or list of paths")
    return tuple(_path(item, base, label, directory) for item in value)


def _parse(data, base: Path) -> Codebases:
    if not isinstance(data, dict) or set(data) != {"libraries"}:
        raise ValueError("Codebase configuration must contain only a top-level 'libraries' list")
    rows = data["libraries"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("libraries must be a non-empty list")
    libraries = []
    modules = set()
    for i, row in enumerate(rows):
        label = f"libraries[{i}]"
        allowed = {"module", "source", "examples", "description", "python_paths"}
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError(f"{label} must be a mapping with fields: {', '.join(sorted(allowed))}")
        module = row.get("module")
        if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", module):
            raise ValueError(f"{label}.module must be a dotted Python module name")
        if module in modules:
            raise ValueError(f"Duplicate library module: {module}")
        modules.add(module)
        source = _path(row.get("source"), base, f"{label}.source")
        description = row.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"{label}.description must be a string")
        libraries.append(Codebase(
            module=module, source=source, description=description,
            examples=_paths(row.get("examples", []), base, f"{label}.examples", False),
            python_paths=_paths(row.get("python_paths", []), base, f"{label}.python_paths", True),
        ))
    return Codebases(tuple(libraries))


def load_codebases(path: Path | None = None) -> Codebases:
    """Explicit YAML overrides legacy module/root settings; blank means legacy."""
    raw = os.getenv("QUANT_MCP_CODEBASES_FILE", "").strip()
    if path is None and not raw:
        root = Path(os.getenv("QUANT_MCP_REPO_ROOT") or PROJECT_ROOT).expanduser().resolve()
        modules = os.getenv("QUANT_MCP_LIBRARY_MODULES", "quant_mcp.pricing")
        examples = [str(root / "examples")] if (root / "examples").exists() else []
        return _parse({"libraries": [
            {"module": m.strip(), "source": str(root), "examples": examples}
            for m in modules.split(",") if m.strip()
        ]}, PROJECT_ROOT)
    config_path = Path(path if path is not None else raw).expanduser()
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    config_path = config_path.resolve()
    try:
        data = yaml.load(config_path.read_text(), Loader=_UniqueKeysLoader)
        return _parse(data, config_path.parent)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid codebase configuration {config_path}: {exc}") from exc


def load_task_codebases(task_dir: Path) -> Codebases:
    snapshot = task_dir / "codebases.json"
    if snapshot.exists():
        return _parse(json.loads(snapshot.read_text()), task_dir)
    return load_codebases()


def snapshot_codebases(task_dir: Path) -> Codebases:
    config = load_codebases()
    (task_dir / "codebases.json").write_text(json.dumps(config.to_dict(), indent=2) + "\n")
    return config
