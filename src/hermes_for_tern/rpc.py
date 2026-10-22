"""Private, bidirectional JSON-RPC connection to an owned Hermes backend."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
from collections import deque
from concurrent.futures import Future
from pathlib import Path


class RpcError(RuntimeError):
    def __init__(self, error: dict):
        self.code = error.get("code")
        super().__init__(str(error.get("message", "Hermes request failed")))


class Backend:
    """Only the reader thread consumes stdout; the UI never blocks on a model call."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str] | None = None):
        child_env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        self.events: queue.Queue[dict] = queue.Queue()
        self.logs: deque[str] = deque(maxlen=80)
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._closing = False
        self._outgoing: queue.Queue[dict | None] = queue.Queue()
        self._writer_thread = threading.Thread(target=self._send, daemon=True)
        self._writer_thread.start()
        self._reader_thread = threading.Thread(target=self._read, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()

    def request(self, method: str, params: dict | None = None) -> Future:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            future: Future = Future()
            self._pending[rid] = future
            try:
                self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            except (OSError, ValueError) as exc:
                self._pending.pop(rid, None)
                future.set_exception(exc)
            return future

    def answer(self, rid: str, result: dict | None = None, *, error: dict | None = None) -> None:
        self._write(
            {"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result or {}})}
        )

    def _write(self, frame: dict) -> None:
        if not self.process.stdin or self.process.poll() is not None:
            raise BrokenPipeError("Hermes backend has exited")
        self._outgoing.put(frame)

    def forget(self, future: Future) -> None:
        """A UI timeout relinquishes its request; a late response cannot change sessions."""
        with self._lock:
            for rid, pending in list(self._pending.items()):
                if pending is future:
                    self._pending.pop(rid)
                    future.cancel()
                    break

    def _send(self) -> None:
        assert self.process.stdin
        try:
            while (frame := self._outgoing.get()) is not None:
                self.process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError):
            if not self._closing:
                self.events.put({"local_error": "Hermes backend stopped accepting requests"})

    def _read(self) -> None:
        assert self.process.stdout
        try:
            for line in self.process.stdout:
                try:
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise ValueError("RPC frame must be an object")
                except ValueError:
                    self.events.put({"local_error": "Hermes emitted an invalid RPC frame"})
                    continue
                if "method" in frame:
                    self.events.put(frame)
                    continue
                rid = frame.get("id")
                if not isinstance(rid, (int, str)):
                    self.events.put({"local_error": "Hermes emitted an invalid RPC response id"})
                    continue
                with self._lock:
                    future = self._pending.pop(rid, None)
                if future and not future.done():
                    if "error" in frame:
                        error = frame["error"]
                        future.set_exception(
                            RpcError(error if isinstance(error, dict) else {"message": "Invalid RPC error"})
                        )
                    else:
                        future.set_result(frame.get("result", {}))
        finally:
            with self._lock:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(BrokenPipeError("Hermes backend disconnected"))
                self._pending.clear()
            if not self._closing:
                self.events.put({"local_error": "Hermes backend disconnected"})

    def _stderr(self) -> None:
        assert self.process.stderr
        for line in self.process.stderr:
            # Keep diagnostics in memory; never print backend output into the surface PTY.
            self.logs.append(line.rstrip())

    def close(self, session_id: str | None = None) -> None:
        self._closing = True
        if self.process.poll() is None and session_id:
            try:
                self.request("session.close", {"session_id": session_id}).result(timeout=2)
            except Exception:
                pass
        self._outgoing.put(None)
        self._writer_thread.join(timeout=0.2)
        # A blocked pipe writer must never hold the UI or prevent process termination.
        if not self._writer_thread.is_alive() and self.process.stdin:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self._terminate_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._terminate_group(signal.SIGKILL)
                self.process.wait(timeout=2)
        # Descendant tools can outlive the gateway; close the owned group as well.
        self._terminate_group(signal.SIGTERM)
        for thread in (self._writer_thread, self._reader_thread, self._stderr_thread):
            thread.join(timeout=0.2)
        if any(
            thread.is_alive() for thread in (self._writer_thread, self._reader_thread, self._stderr_thread)
        ):
            self._terminate_group(signal.SIGKILL)
        for thread in (self._writer_thread, self._reader_thread, self._stderr_thread):
            thread.join(timeout=1)
        if self.process.stdin:
            self.process.stdin.close()
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()

    def _terminate_group(self, sig: int) -> None:
        try:
            if os.name == "posix":
                # start_new_session gives this backend its own process group.
                os.killpg(self.process.pid, sig)
            elif sig == signal.SIGTERM:
                self.process.terminate()
            else:
                self.process.kill()
        except ProcessLookupError:
            pass
