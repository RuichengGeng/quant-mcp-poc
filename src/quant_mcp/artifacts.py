"""Bounded, transport-independent access to a single server's task artifacts."""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def task_dir(self, task_id: str) -> Path:
        if not task_id.startswith("task_") or Path(task_id).name != task_id:
            raise ValueError("Invalid task_id")
        path = self.root / task_id
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Unknown task_id")
        return path

    def resolve(self, task_id: str, filename: str) -> Path:
        task = self.task_dir(task_id)
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts or not filename:
            raise ValueError("Artifact must be a relative path within the task")
        path = task / relative
        # Reject symlinks even when their current target is inside the task.
        if any(p.is_symlink() for p in (path, *path.parents) if p != self.root):
            raise ValueError("Artifact symlinks are not supported")
        if not path.is_file():
            raise ValueError("Unknown artifact")
        return path

    def list_tasks(self, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not self.root.exists():
            return []
        rows = []
        for path in sorted(self.root.glob("task_*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                rows.append({"task_id": path.name})
            if len(rows) >= limit:
                break
        return rows

    def list_files(self, task_id: str, offset: int = 0, limit: int = 100) -> dict:
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("offset must be non-negative; limit must be 1..500")
        task = self.task_dir(task_id)
        files = []
        for path in sorted(task.rglob("*")):
            try:
                path = self.resolve(task_id, path.relative_to(task).as_posix())
            except ValueError:
                continue
            files.append({"filename": path.relative_to(task).as_posix(),
                          "size_bytes": path.stat().st_size,
                          "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"})
        return {"task_id": task_id, "files": files[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(files) else None}

    def read(self, task_id: str, filename: str, offset: int = 0,
             max_bytes: int = 65536, encoding: str = "utf-8") -> dict:
        if offset < 0 or not 1 <= max_bytes <= 262144:
            raise ValueError("offset must be non-negative; max_bytes must be 1..262144")
        if encoding not in {"utf-8", "base64"}:
            raise ValueError("encoding must be utf-8 or base64")
        path = self.resolve(task_id, filename)
        with path.open("rb") as stream:
            size = path.stat().st_size
            stream.seek(offset)
            data = stream.read(max_bytes)
        if encoding == "utf-8":
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                # A page may end partway through a multi-byte character.
                if exc.reason == "unexpected end of data" and exc.start > 0 and offset + len(data) < size:
                    data = data[:exc.start]
                    content = data.decode("utf-8")
                else:
                    raise ValueError("Cannot decode this chunk as UTF-8; use base64 or a larger chunk") from exc
        else:
            content = base64.b64encode(data).decode("ascii")
        end = offset + len(data)
        return {"task_id": task_id, "filename": filename, "encoding": encoding,
                "content": content, "size_bytes": size, "offset": offset,
                "next_offset": end if end < size else None}

    def result(self, task_id: str) -> dict:
        page = self.read(task_id, "result.json", max_bytes=262144)
        if page["next_offset"] is not None:
            raise ValueError("Result exceeds inline limit; read result.json in chunks")
        result = json.loads(page["content"])
        if not isinstance(result, dict):
            raise ValueError("Invalid result object")
        return result
