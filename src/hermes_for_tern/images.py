"""Bounded clipboard snapshots and private PNG/JPEG storage. No gateway staging here."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image

from .messages import ImageRef, Message
from .recovery import RecoveryStore

PIXELS = 20_000_000
RETENTION = 7 * 86400


def dropped_image_path(text: str) -> str | None:
    """Recognize one local image path, including shell-escaped terminal drops. Never run a shell."""
    raw = text.strip()
    if not raw or "\n" in raw or "\r" in raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if len(parts) == 1:
        path = parts[0]
    elif (
        raw.startswith(("/", "~/", "./", "../"))
        and "/" in parts[0].lstrip("/")
        and not any(part.startswith(("/", "~/", "./", "../", "file://")) for part in parts[1:])
    ):
        # Native editors may provide an unescaped absolute path with spaces.
        path = raw
    else:
        return None
    if path.startswith("file://"):
        url = urlsplit(path)
        if url.netloc not in ("", "localhost") or url.query or url.fragment:
            return None
        path = unquote(url.path)
    if not path.startswith(("/", "~/", "./", "../")):
        return None
    return path if Path(path).suffix.lower() in (".png", ".jpg", ".jpeg") else None


def limit(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if not 1 <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(f"{name} must be between 1 and {maximum}.") from None


class ImageStore:
    def __init__(self, cwd: Path, profile: str | None):
        self.directory = RecoveryStore(cwd, profile).directory / "images"
        self.max_bytes = limit("HERMES_TERN_IMAGE_MAX_BYTES", 8 << 20, 16 << 20)
        self.max_count = limit("HERMES_TERN_IMAGE_MAX_COUNT", 4, 16)

    def path(self, image: ImageRef) -> Path:
        return self.directory / (image.id + (".png" if image.media == "image/png" else ".jpg"))

    def preview_path(self, image: ImageRef) -> Path:
        return self.directory / (image.id + ".thumb.png")

    def add_path(self, path: Path) -> ImageRef:
        if not path.is_file():
            raise ValueError("Choose an existing PNG or JPEG file.")
        if path.stat().st_size > self.max_bytes:
            raise ValueError(f"Image exceeds the {self.max_bytes // (1 << 20)} MiB limit.")
        with path.open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        return self.add(data, path.name)

    def add(self, data: bytes, name: str = "Clipboard image") -> ImageRef:
        if not data or len(data) > self.max_bytes:
            raise ValueError(f"Image is empty or exceeds the {self.max_bytes // (1 << 20)} MiB limit.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as source:
                    if source.format not in ("PNG", "JPEG"):
                        raise ValueError("Only PNG and JPEG images are supported.")
                    width, height = source.size
                    if width * height > PIXELS or width < 1 or height < 1:
                        raise ValueError("Image exceeds the 20 megapixel limit.")
                    media = Image.MIME[source.format]
                    source.verify()
                with Image.open(io.BytesIO(data)) as source:
                    source.load()
                    thumb = source.convert("RGB")
                    thumb.thumbnail((96, 72))
                    preview = io.BytesIO()
                    thumb.save(preview, format="PNG")
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning, SyntaxError):
            raise ValueError("Image is damaged, unsupported, or too large to decode.") from None
        image = ImageRef(
            hashlib.sha256(data).hexdigest(),
            Path(name).name[:160] or "Image",
            media,
            width,
            height,
            len(data),
        )
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write(self.path(image), data)
        self._write(self.preview_path(image), preview.getvalue())
        return image

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        fd, name = tempfile.mkstemp(prefix=".image-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def read(self, image: ImageRef) -> bytes:
        with self.path(image).open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        if (
            len(data) != image.size
            or len(data) > self.max_bytes
            or hashlib.sha256(data).hexdigest() != image.id
        ):
            raise ValueError(f"{image.name}: saved image is missing, changed, or exceeds the current limit.")
        return data

    def collect(self) -> None:
        """Only old, unreferenced adapter-owned files; never originals or Hermes-owned files."""
        referenced = set()
        for path in self.directory.parent.glob("*.json"):
            try:
                record = json.loads(path.read_text())
                if record.get("version") not in (1, 2):
                    return
                values = [
                    record.get("pending") or "",
                    {"text": "", "images": record.get("draft_images", [])},
                    *record.get("queue", []),
                ]
                for value in values:
                    referenced.update(image.id for image in Message.read(value).images)
            except (OSError, ValueError, TypeError, AttributeError):
                # Unknown/corrupt recovery data is not a license to delete its files.
                return
        for path in self.directory.glob("*"):
            if (
                re.fullmatch(r"[a-f0-9]{64}\.(?:png|jpg|thumb\.png)", path.name)
                and path.name.split(".", 1)[0] not in referenced
                and not path.is_symlink()
                and path.is_file()
            ):
                if path.stat().st_mtime < time.time() - RETENTION:
                    path.unlink(missing_ok=True)


def clipboard_image(max_bytes: int) -> bytes:
    """Read only on an explicit paste action. Remote shells cannot read the client's clipboard."""
    if any(os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")):
        raise ValueError(
            "Image clipboard access is unavailable over SSH. Use Attach image with a local file path."
        )
    with tempfile.TemporaryDirectory(prefix="hermes-image-") as directory:
        path = Path(directory) / "clipboard.png"
        path.touch(mode=0o600)
        if sys.platform == "darwin":
            script = """on run argv
set destination to POSIX file (item 1 of argv)
set picture to the clipboard as «class PNGf»
set handle to open for access destination with write permission
try
set eof handle to 0
write picture to handle
close access handle
on error problem
close access handle
error problem
end try
end run"""
            commands = [(["osascript", "-e", script, str(path)], False)]
        else:
            commands = []
            if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
                commands += [
                    (["wl-paste", "--no-newline", "--type", media], True)
                    for media in ("image/png", "image/jpeg")
                ]
            if shutil.which("xclip"):
                commands += [
                    (["xclip", "-selection", "clipboard", "-t", media, "-o"], True)
                    for media in ("image/png", "image/jpeg")
                ]
        if not commands:
            raise ValueError(
                "Clipboard images need wl-paste (Wayland) or xclip (X11). Use Attach image instead."
            )
        overall_deadline = time.monotonic() + 6
        for command, pipe in commands:
            if time.monotonic() >= overall_deadline:
                break
            path.write_bytes(b"")
            try:
                with path.open("wb") as output:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=output if pipe else subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        deadline = min(time.monotonic() + 5, overall_deadline)
                        while process.poll() is None:
                            if path.stat().st_size > max_bytes or time.monotonic() >= deadline:
                                process.kill()
                                break
                            time.sleep(0.02)
                        process.wait(timeout=1)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=1)
                if path.stat().st_size > max_bytes:
                    raise ValueError("Clipboard image exceeds the configured size limit.")
                if process.returncode == 0 and path.stat().st_size:
                    return path.read_bytes()
            except OSError:
                continue
    raise ValueError("No readable PNG/JPEG image in the clipboard. Use Attach image to choose a file.")
