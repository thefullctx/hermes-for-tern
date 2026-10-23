"""Drive the real App in an isolated headless Tern using the controlled QA backend.

Run: uv run python scripts/check_native.py [output-directory]
No model calls, project edits, or changes to the user's Tern/Hermes configuration.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def native_app(clipboard_path: str) -> int:
    import tern_sdk

    from hermes_for_tern.app import App

    session = tern_sdk.connect(app="hermes", version="0.1.0", features=("edit", "undo", "send"))
    if session is None:
        raise RuntimeError("Native QA needs Tern")
    command = [sys.executable, "-u", str(ROOT / "tests/fixtures/recovery_backend.py")]
    instance = App(session, command, ROOT, {})
    # Headless Tern's clipboard is process-local. Exercise its real empty paste signal,
    # with the OS clipboard reader supplied by a fixture; never replace the user's clipboard.
    instance.clipboard_reader = lambda _: Path(clipboard_path).read_bytes()
    return instance.run()


class Check:
    def __init__(self, out: Path):
        self.out = out
        self.endpoint = f"/tmp/hermes-tern-qa-{os.getpid()}.sock"
        self.seq = 0

    def ctl(self, *args) -> dict:
        result = subprocess.run(
            ["tern", "ctl", "--control", self.endpoint, *args],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=ROOT,
        )
        if result.returncode or '"ok":false' in result.stdout:
            raise RuntimeError(result.stdout + result.stderr)
        return json.loads(result.stdout) if not args or args[0] != "--file" else {}

    def scenario(self, text: str) -> None:
        self.seq += 1
        path = self.out / f"control-{self.seq}.txt"
        path.write_text(text + "\n")
        self.ctl("--file", str(path))

    def nodes(self) -> list[dict]:
        return self.ctl("tree", ".sf").get("nodes", [])

    def wait(self, text: str, *, absent: bool = False) -> list[dict]:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            nodes = self.nodes()
            present = any(text in node.get("text", "") for node in nodes)
            if present != absent:
                return nodes
            time.sleep(0.08)
        raise RuntimeError(f"Native check timed out: {text!r}, absent={absent}")

    def send(self, text: str) -> None:
        self.scenario(f"type {json.dumps(text)}\nkey enter")

    def wait_images(self, count: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            buttons = [n for n in self.nodes() if n.get("tag") == "button" and n.get("text") == "Remove"]
            if len(buttons) == count:
                return
            time.sleep(0.08)
        raise RuntimeError(f"Expected {count} removable draft images")

    def click(self, label: str) -> None:
        nodes = self.wait(label)
        node = next(node for node in nodes if node.get("tag") == "button" and label in node.get("text", ""))
        x, y, width, height = node["rect"]
        self.scenario(f"click {x + width / 2} {y + height / 2}")

    def check_image_preview(self, width: int, height: int) -> None:
        frame = next(n for n in self.ctl("tree", ".sf-img-frame")["nodes"] if n["rect"][2:4] == [96.0, 72.0])
        x, y, w, h = frame["rect"]
        self.scenario(f"click {x + w / 2} {y + h / 2}")
        deadline = time.monotonic() + 5
        while True:
            preview = self.ctl("state").get("lightbox")
            if preview and preview.get("rect"):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Image preview did not open")
            time.sleep(0.08)
        assert not preview["failed"]
        scale = preview["zoom"] / 100
        assert abs(preview["rect"][2] / scale - width) < 1
        assert abs(preview["rect"][3] / scale - height) < 1
        time.sleep(0.2)
        self.scenario('shot "image-preview-fit"\nclick ".ed-zoom"')
        deadline = time.monotonic() + 5
        while True:
            options = self.ctl("tree", ".mi").get("nodes", [])
            actual = next((node for node in options if node.get("text") == "100%"), None)
            if actual:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Image zoom menu did not open")
            time.sleep(0.08)
        x, y, w, h = actual["rect"]
        self.scenario(f"click {x + w / 2} {y + h / 2}")
        deadline = time.monotonic() + 5
        while True:
            preview = self.ctl("state")["lightbox"]
            if preview["zoom"] == 100 and preview.get("rect", [])[2:4] == [float(width), float(height)]:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Image did not zoom to 100%")
            time.sleep(0.08)
        assert preview["rect"][2:4] == [float(width), float(height)]
        time.sleep(0.2)
        self.scenario('shot "image-preview-100"\nkey escape')
        deadline = time.monotonic() + 5
        while self.ctl("state").get("lightbox"):
            if time.monotonic() > deadline:
                raise RuntimeError("Image preview did not close")
            time.sleep(0.08)

    def run(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        from PIL import Image, ImageDraw, ImageFont

        image_path = self.out / "native-clipboard.png"
        Image.new("RGB", (160, 100), "red").save(image_path)
        dropped_path = self.out / "Screenshot 2026-10-23 at 12.00.00.png"
        dropped_path.write_bytes(image_path.read_bytes())
        detail_path = self.out / "Full resolution detail.png"
        detail = Image.new("RGB", (1800, 1200), "white")
        draw = ImageDraw.Draw(detail)
        font = ImageFont.load_default(size=28)
        for y in range(0, 1200, 60):
            draw.line((0, y, 1800, y), fill="#b0bdcd", width=1)
            draw.text(
                (24, y + 15),
                f"Full-resolution row {y // 60:02d}   ABCDEFGHIJKLMNOPQRSTUVWXYZ   0123456789",
                font=font,
                fill="#17233a",
            )
        detail.save(detail_path)
        state_home = self.out / "draft-state" / str(os.getpid())
        env = {
            **os.environ,
            "TERN_CONFIG_DIR": str(self.out / "tern-config"),
            "XDG_STATE_HOME": str(state_home),
        }
        with (self.out / "tern.log").open("w") as log:
            server = subprocess.Popen(
                ["tern", "serve", "--control", self.endpoint, "--out", str(self.out)],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 15
                while not Path(self.endpoint).exists():
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f"Tern did not start; see {self.out / 'tern.log'}")
                    time.sleep(0.08)
                command = shlex.join(
                    [sys.executable, str(Path(__file__).resolve()), "--app", str(image_path)]
                )
                self.scenario(f"account signed-in\nsize 1200 900\nready\nrun {json.dumps(command)}")
                self.wait("What would you like to work on?")
                self.scenario('shot "composer-idle"')
                self.scenario(f'drop ".sf-editor" file {json.dumps(str(detail_path))}')
                self.wait_images(1)
                self.send("preview detail")
                self.wait("Received: preview detail · images=1")
                self.check_image_preview(1800, 1200)
                self.scenario(f'drop ".sf-editor" file {json.dumps(str(dropped_path))}')
                self.wait_images(1)
                self.wait(dropped_path.name)
                self.send("dragged screenshot")
                self.wait("Received: dragged screenshot · images=1")
                escaped = str(dropped_path).replace(" ", r"\ ") + " "
                self.scenario(f"clipboard text {json.dumps(escaped)}\nkey meta+v")
                self.wait_images(1)
                self.wait(dropped_path.name)
                self.scenario('shot "image-path-drop"')
                self.send("dropped screenshot")
                self.wait("Received: dropped screenshot · images=1")
                assert not any("Unknown command" in node.get("text", "") for node in self.nodes())
                self.send("/attach")
                self.wait("Image file path")
                self.send(str(image_path))
                self.wait("native-clipboard.png")
                self.scenario('shot "image-path"')
                self.click("Remove")
                self.wait("native-clipboard.png", absent=True)
                self.send(f"/attach {image_path}")
                self.wait("native-clipboard.png")
                self.click("Remove")
                self.wait_images(0)
                self.send("/paste-image")
                self.wait("Clipboard image")
                self.click("Remove")
                self.wait_images(0)
                self.scenario(f'clipboard flavor "image/png" {json.dumps(str(image_path))}\nkey meta+v')
                self.wait("Clipboard image")
                time.sleep(0.3)
                self.scenario('shot "image-draft"\nsize 560 760\nshot "image-draft-narrow"')
                self.scenario('appearance dark\nshot "image-draft-dark"')
                for count in range(2, 5):
                    self.scenario("key meta+v")
                    self.wait_images(count)
                self.scenario('shot "image-multiple-narrow"')
                for count in (3, 2, 1):
                    self.click("Remove")
                    self.wait_images(count)
                self.scenario("size 1200 900")
                self.scenario("key enter")
                self.wait("Received: [User attached images] · images=1")
                self.send("plain after image")
                self.wait("Received: plain after image")
                assert not any(
                    "Received: plain after image · images=" in n.get("text", "") for n in self.nodes()
                )
                self.send("wait")
                self.wait("Waiting for Stop")
                self.scenario("key meta+v")
                self.wait("Remove")
                self.send("queued with image")
                self.wait("queued ·")
                self.scenario("key escape")
                self.wait("Queue paused")
                self.click("Continue queue")
                self.wait("Received: queued with image · images=1")
                self.send("approval")
                self.wait("Permission needed")
                self.scenario('shot "approval"\nkey enter')
                self.wait("Native answer received.")
                self.send("clarify")
                self.wait("Choose a test answer")
                self.scenario("key 2")
                self.wait("Choose a test answer", absent=True)
                self.send("wait")
                self.wait("Waiting for Stop")
                self.send("follow-up after review")
                self.wait("queued ·")
                self.scenario("key escape")
                self.wait("Queue paused")
                self.scenario('shot "queue-paused"')
                self.click("Discard queue")
                self.wait("Queue paused", absent=True)
                self.send("crash")
                self.wait("Reconnect and resume")
                self.scenario('shot "recovery"')
                self.click("Reconnect and resume")
                self.wait("Reconnected.")
                self.send("delegation")
                self.wait("Received: delegation")
                self.send("/find restored")
                self.wait("matches")
                self.scenario('shot "search"\nsize 560 760\nshot "search-narrow"')
                self.scenario('appearance dark\nshot "search-narrow-dark"')
                # Let native theme changes settle before recording a second viewport.
                self.nodes()
                self.scenario('size 1200 900\nshot "search-dark"')
                self.send("/find-close")
                self.send("wait")
                self.wait("Waiting for Stop")
                self.send("survives restart")
                self.wait("queued ·")
                self.scenario("key escape")
                self.wait("Queue paused")
                self.scenario('type "unsent draft after restart"')
                self.wait("unsent draft after restart")
                self.scenario("key meta+v")
                self.wait("Remove")
                deadline = time.monotonic() + 5
                record = None
                while record is None:
                    for path in state_home.rglob("*.json"):
                        data = json.loads(path.read_text())
                        if data.get("draft") == "unsent draft after restart" and data.get("draft_images"):
                            record = data
                            break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Frontend draft was not persisted")
                    time.sleep(0.08)
                # This unique QA state directory identifies only the frontend we launched.
                os.kill(record["pid"], signal.SIGTERM)
                self.wait("Native QA · controlled backend", absent=True)
                self.scenario(f"ready\nrun {json.dumps(command)}")
                self.wait("Restore saved text")
                self.click("Restore saved text")
                self.wait("unsent draft after restart")
                self.wait("Queue paused")
                self.wait("Remove")
                self.scenario('shot "draft-restored"')
                assert not any("Received: survives restart" in node.get("text", "") for node in self.nodes())
                self.scenario("key ctrl+u")
                self.send("/quit")
                self.wait("Native QA · controlled backend", absent=True)
                assert self.ctl("state")["focused"]["last"]["status"] == 0
                print(
                    f"Native full-resolution preview/100% zoom, image path/paste, image-only send, queued images, approval, clarification, crash/resume, search and image draft restoration: OK. Screenshots: {self.out / 'serve'}"
                )
            finally:
                try:
                    self.ctl("quit")
                finally:
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=3)
                    Path(self.endpoint).unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--app"]:
        raise SystemExit(native_app(sys.argv[2]))
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "work/native-qa"
    Check(output.resolve()).run()
