from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AgentResult:
    status: str
    message: str
    executed: bool = False


class CodingAgent(Protocol):
    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        ...
