"""A labelled, timed simulation for the native frontend. Never runs Hermes or tools."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time


class Demo:
    def __init__(self, duration: float):
        self.duration = duration
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None

    def send(self, **frame):
        with self.lock:
            print(json.dumps({"jsonrpc": "2.0", **frame}), flush=True)

    def event(self, kind: str, payload: dict | None = None):
        self.send(method="event", params={"session_id": "demo", "type": kind, "payload": payload or {}})

    def run_demo(self):
        phases = [
            (
                "project_scan",
                "Scanning the project",
                "Mapping modules and finding entry points",
                "12 modules indexed",
            ),
            (
                "read_file",
                "Reading the code",
                "Tracing data flow and checking assumptions",
                "8 files inspected",
            ),
            (
                "edit_file",
                "Sketching a change",
                "Preparing a small refactor and reviewing its shape",
                "3 simulated changes prepared",
            ),
            (
                "terminal",
                "Checking the result",
                "Walking through the pretend test suite",
                "24 simulated checks passed",
            ),
            (
                "build_preview",
                "Preparing the preview",
                "Checking the final layout and polishing the handoff",
                "Preview ready",
            ),
        ]
        narratives = [
            "I’m mapping the simulated project’s modules and entry points. The next pass follows imports, groups related components, and identifies the routes through the application. I’m also checking configuration boundaries and shared utilities before collecting the results into a small project map.",
            "I’m following a simulated request through the handlers and their supporting functions. This pass compares the expected inputs with the values passed between modules. I’m checking the error paths, looking at how state is updated, and gathering a short list of places that deserve closer review.",
            "I’m sketching a simulated refactor that keeps the public interface small. The draft groups repeated logic into a helper and makes the error paths easier to follow. I’m reviewing each proposed change against the earlier project map and preparing a concise explanation of the tradeoffs.",
            "I’m stepping through simulated checks for the happy path and the edge cases. The preview checks include empty inputs, interrupted requests, and failures in a dependency. I’m comparing the results with the intended behavior and collecting a compact report for the handoff.",
            "I’m assembling the simulated preview and its summary. This final pass checks the layout, reviews the proposed changes, and collects the completed checks in one place. I’m polishing the explanation so it is clear what was simulated and what a real implementation would need next.",
        ]
        started = time.monotonic()
        self.event("message.start")
        for index, (tool, title, detail, result) in enumerate(phases):
            tid = f"demo-{index}"
            self.event(
                "tool.start", {"tool_id": tid, "name": tool, "args": {"path": "SIMULATED · no files touched"}}
            )
            self.event("status.update", {"text": title})
            self.event("message.delta", {"text": f"### {title}\n\n"})
            end = started + self.duration * (index + 1) / len(phases)
            phase_start = started + self.duration * index / len(phases)
            words = narratives[index].split()
            sent_words = 0
            while time.monotonic() < end:
                if self.stop.wait(min(1.5, max(0, end - time.monotonic()))):
                    self.event("message.complete", {"status": "interrupted"})
                    return
                self.event("demo.progress", {"value": (time.monotonic() - started) / self.duration})
                count = min(
                    len(words), int((time.monotonic() - phase_start) / (end - phase_start) * len(words))
                )
                if count > sent_words:
                    self.event("message.delta", {"text": " ".join(words[sent_words:count]) + " "})
                    sent_words = count
            self.event("message.interim", {"text": f"### {title}\n\n{narratives[index]}"})
            self.event(
                "tool.complete",
                {"tool_id": tid, "result": {"output": result}, "duration_s": self.duration / 5},
            )
        self.event("demo.progress", {"value": 1.0})
        self.event(
            "message.delta",
            {
                "text": "## Demo complete\n\nFive simulated phases finished. No model calls, commands, or file changes were made."
            },
        )
        self.event(
            "message.complete",
            {
                "status": "complete",
                "text": "## Demo complete\n\nFive simulated phases finished. No model calls, commands, or file changes were made.",
            },
        )

    def run(self):
        self.event("gateway.ready")
        for line in sys.stdin:
            frame = json.loads(line)
            rid, method = frame.get("id"), frame.get("method")
            if method == "client.capabilities":
                self.send(id=rid, result={"server_requests": []})
            elif method == "session.create":
                self.send(
                    id=rid,
                    result={
                        "session_id": "demo",
                        "info": {"model": "Visual demo · no model calls", "demo": True},
                    },
                )
            elif method == "prompt.submit":
                if self.worker and self.worker.is_alive():
                    self.send(id=rid, error={"code": -32000, "message": "Demo is already running"})
                    continue
                self.stop.clear()
                self.send(id=rid, result={"status": "streaming"})
                self.worker = threading.Thread(target=self.run_demo, daemon=True)
                self.worker.start()
            elif method == "session.interrupt":
                self.stop.set()
                self.send(id=rid, result={"status": "interrupted"})
            elif method == "session.close":
                self.stop.set()
                if self.worker:
                    self.worker.join(timeout=2)
                self.send(id=rid, result={"closed": True})
                return
            else:
                self.send(id=rid, error={"code": -32601, "message": "Unsupported demo request"})
        self.stop.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=300)
    args = parser.parse_args()
    if not 0 < args.duration <= 1800:
        parser.error("duration must be positive and at most 1800 seconds")
    Demo(args.duration).run()


if __name__ == "__main__":
    main()
