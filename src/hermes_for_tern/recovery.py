"""Private, atomic storage of unsent ordinary text; never serialize question answers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from .messages import ImageRef, Message


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".draft-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class RecoveryStore:
    def __init__(self, cwd: Path, profile: str | None):
        root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        scope = json.dumps([str(cwd.resolve()), profile or "default"])
        self.directory = root / "hermes-for-tern" / hashlib.sha256(scope.encode()).hexdigest()[:24]
        # Each pane owns its own record, so concurrent panes cannot overwrite one another.
        self.path = self.directory / f"{uuid.uuid4().hex}.json"
        self.last: dict | None = None

    def records(self) -> list[dict]:
        records = []
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if (
                    not isinstance(data, dict)
                    or data.get("version") not in (1, 2)
                    or not isinstance(data.get("draft"), str)
                    or not isinstance(data.get("queue"), list)
                    or not all(isinstance(text, (str, dict)) for text in data["queue"])
                    or not isinstance(data.get("session"), (str, type(None)))
                    or not isinstance(data.get("saved"), (int, float))
                ):
                    continue
                for message in data["queue"]:
                    Message.read(message)
                for image in data.get("draft_images", []):
                    ImageRef.read(image)
                if data.get("pending") is not None:
                    Message.read(data["pending"])
                pid = data.get("pid")
                if isinstance(pid, int) and pid > 0:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        continue
                    else:
                        continue  # another live pane still owns this draft
                if (
                    data["draft"]
                    or data["queue"]
                    or data.get("uncertain")
                    or data.get("draft_images")
                    or data.get("pending")
                ):
                    records.append({**data, "path": str(path)})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(records, key=lambda record: record["saved"], reverse=True)

    def save(
        self,
        draft: str,
        queued: list[str | Message],
        session: str | None,
        uncertain: bool,
        *,
        images: tuple[ImageRef, ...] = (),
        pending: Message | None = None,
    ) -> None:
        data = {
            "version": 2,
            "draft": draft,
            "queue": [Message.read(message).wire() for message in queued],
            "draft_images": [image.wire() for image in images],
            "pending": pending.wire() if pending is not None else None,
            "session": session,
            "uncertain": uncertain,
        }
        if data == self.last:
            return
        if draft or queued or uncertain or images or pending:
            private_write(
                self.path,
                json.dumps({**data, "saved": time.time(), "pid": os.getpid()}, ensure_ascii=False) + "\n",
            )
        else:
            self.path.unlink(missing_ok=True)
        self.last = data

    def forget(self, record: dict) -> None:
        path = Path(record["path"])
        if path.parent == self.directory and path.suffix == ".json":
            path.unlink(missing_ok=True)
