import sys
import time
from pathlib import Path

from hermes_for_tern.rpc import Backend


def demo(tmp_path: Path, duration: str):
    backend = Backend(
        [sys.executable, "-u", "-m", "hermes_for_tern.demo_backend", "--duration", duration], tmp_path
    )
    backend.request("client.capabilities", {}).result(timeout=2)
    assert backend.request("session.create", {}).result(timeout=2)["info"]["demo"]
    backend.request("prompt.submit", {"text": "start"}).result(timeout=2)
    return backend


def collect_until_complete(backend):
    frames = []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        frame = backend.events.get(timeout=2)
        if frame.get("method") == "event":
            frames.append(frame["params"])
            if frame["params"]["type"] == "message.complete":
                return frames
    raise AssertionError("Demo did not complete")


def test_demo_finishes_all_five_simulated_phases(tmp_path):
    backend = demo(tmp_path, "0.5")
    try:
        frames = collect_until_complete(backend)
        assert sum(f["type"] == "tool.start" for f in frames) == 5
        assert sum(f["type"] == "tool.complete" for f in frames) == 5
        assert frames[-1]["payload"]["status"] == "complete"
        assert any(f["type"] == "demo.progress" and f["payload"]["value"] == 1 for f in frames)
    finally:
        backend.close("demo")


def test_demo_interrupt_stops_the_worker_without_finishing_remaining_phases(tmp_path):
    backend = demo(tmp_path, "30")
    try:
        backend.request("session.interrupt", {}).result(timeout=2)
        frames = collect_until_complete(backend)
        assert frames[-1]["payload"]["status"] == "interrupted"
        assert sum(f["type"] == "tool.complete" for f in frames) == 0
    finally:
        backend.close("demo")
