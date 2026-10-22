"""Private stdio client of Hermes's shared gateway; never a second database writer.

The official bootstrap discovers the owner and mints its short-lived socket ticket.
Older Hermes builds without that bootstrap retain their original stdio backend.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from .rpc import RpcError

BOOTSTRAP = "ui-tui.scripts.gateway_bootstrap"


def bootstrap(original: str) -> dict | None:
    deadline = time.monotonic() + 40
    for start in (False, True):
        result = subprocess.run(
            [original, "--run-module", BOOTSTRAP, *(["--start"] if start else [])],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=max(0.1, deadline - time.monotonic()),
        )
        if result.returncode == 0:
            grant = json.loads(result.stdout)
            parsed = urlsplit(grant["url"])
            if (
                parsed.scheme not in ("ws", "wss")
                or not parsed.hostname
                or not ipaddress.ip_address(parsed.hostname).is_loopback
                or parsed.username
                or parsed.password
                or not isinstance(grant.get("protocols"), list)
                or "hermes-gateway-v1" not in grant["protocols"]
            ):
                raise ValueError("Invalid gateway grant")
            return grant
        missing = re.search(r"No module named ['\"]([^'\"]+)['\"]", result.stderr)
        if missing and missing[1] in ("ui-tui", "ui-tui.scripts", BOOTSTRAP):
            return None
    raise RuntimeError("Hermes gateway unavailable. Run the original Hermes doctor, then reconnect.")


def emit(frame: dict) -> None:
    print(json.dumps(frame, ensure_ascii=False), flush=True)


def row_text(content):
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    return "\n".join(
        part
        if isinstance(part, str)
        else str(part.get("text") or ("[image]" if "image" in str(part.get("type")) else ""))
        for part in content
        if isinstance(part, (str, dict))
    )


class Adapter:
    def __init__(self, call):
        self.wire = call
        self.sessions: dict[str, dict] = {}
        self.images: dict[str, list[dict]] = {}
        self.admissions: dict[str, dict] = {}
        self.controls: dict[str, tuple[str, str, dict]] = {}
        self.confirmations: dict[tuple[str, str], str] = {}

    async def scoped(self, method: str, params: dict) -> dict:
        profile = self.sessions.get(params.get("session_id"), {}).get("_profile")
        if profile:
            params = {**params, "profile": profile}
        return await self.wire(method, params)

    def event(self, frame: dict) -> list[dict]:
        params = frame.get("params") or {}
        sid, kind = params.get("session_id"), params.get("type")
        payload = params.get("payload") or {}
        admission = params.get("admission_id") or payload.get("admission_id")
        own = self.admissions.get(sid)
        if kind == "message.complete":
            if own and admission and admission != own["admission_id"]:
                return []  # Another viewer's earlier turn cannot drain our local follow-up queue.
            if own and admission == own["admission_id"]:
                self.admissions.pop(sid, None)
            if payload.get("outcome") == "cancelled":
                frame = {**frame, "params": {**params, "payload": {**payload, "status": "interrupted"}}}
        if sid in self.sessions:
            for key in ("execution_generation", "revision"):
                if key in params or key in payload:
                    self.sessions[sid][key] = params.get(key, payload.get(key))
        if kind in ("approval.request", "clarify.request"):
            method = kind.split(".")[0]
            rid = f"tern-control:{sid}:{payload['prompt_id']}"
            self.controls[rid] = (method, sid, payload)
            values = {**payload, "session_id": sid, "request_id": payload["prompt_id"]}
            if method == "clarify":
                values["questions"] = [{**payload, "qid": payload["prompt_id"]}]
            return [{"jsonrpc": "2.0", "method": method, "id": rid, "params": values}]
        if kind in ("approval.settled", "clarify.settled"):
            rid = f"tern-control:{sid}:{payload['prompt_id']}"
            self.controls.pop(rid, None)
            return [
                {
                    "method": "event",
                    "params": {"session_id": sid, "type": "request.cancel", "payload": {"id": rid}},
                }
            ]
        if kind == "session.replay_gap":
            # Recovery must take a fresh snapshot; silently missing turn outcomes could replay work.
            raise RuntimeError(
                "Gateway event history changed. Reconnect to review the session; nothing will be resent."
            )
        return [frame]

    async def answer(self, frame: dict) -> bool:
        control = self.controls.get(frame.get("id"))
        if control is None:
            return False
        method, sid, prompt = control
        if "error" in frame:
            # A different session/view cannot answer this card. Leave it for its other viewers.
            self.controls.pop(frame["id"], None)
            return True
        result = frame.get("result") or {}
        params = {
            "session_id": sid,
            "execution_generation": prompt["execution_generation"],
            "prompt_id": prompt["prompt_id"],
        }
        if method == "approval":
            params["choice"] = result.get("choice", "deny")
        else:
            params["answer"] = str((result.get("answers") or {}).get(prompt["prompt_id"], ""))
        await self.scoped(method + ".respond", params)
        self.controls.pop(frame["id"], None)
        return True

    async def request(self, method: str, original: dict) -> dict:
        params = dict(original)
        sid = params.get("session_id")
        if method == "client.capabilities":
            result = await self.wire(method, params)
            return {**result, "gateway_protocol": "hermes-gateway-v1"}
        if method in ("session.create", "session.resume"):
            params.pop("cols", None)
            if method == "session.create":
                params.setdefault("request_id", uuid.uuid4().hex)
                # Canonical create treats a title as identity, unlike the old display-only title.
                if params.get("title") == "Hermes for Tern":
                    params.pop("title")
            else:
                params["display_projection"] = True
            try:
                result = await self.wire(method, params)
            except RpcError as exc:
                if method != "session.resume" or str(exc) != "not_found":
                    raise
                params["title"] = params.pop("session_id")
                result = await self.wire(method, params)
            actual = result["session_id"]
            self.sessions[actual] = {**result, "_profile": params.get("profile")}
            result = dict(result)
            result["messages"] = result.get(
                "display_messages",
                [
                    {
                        **row,
                        "text": row.get("text") or row_text(row.get("content")),
                        "name": row.get("tool_name", row.get("name")),
                    }
                    for row in result.get("messages", [])
                    if isinstance(row, dict)
                ],
            )
            return result
        if method == "session.list":
            params["scope"] = "stored"
        elif method == "session.close":
            session = self.sessions.get(sid, {})
            self.images.pop(sid, None)
            self.admissions.pop(sid, None)
            if not session.get("subscription_id"):
                return {}
            result = await self.scoped(
                "session.detach", {"session_id": sid, "subscription_id": session["subscription_id"]}
            )
            self.sessions.pop(sid, None)
            return result
        elif method == "image.attach_bytes":
            result = await self.scoped(method, params)
            if result.get("attached") is not True or not result.get("path") or not result.get("mime"):
                raise ValueError("Invalid image acknowledgement")
            images = self.images.setdefault(sid, [])
            images.append({"path": result["path"], "mime": result["mime"]})
            return {**result, "count": len(images)}
        elif method == "image.detach":
            images = self.images.setdefault(sid, [])
            kept = [image for image in images if image["path"] != params.get("path")]
            self.images[sid] = kept
            return {"detached": len(kept) < len(images), "count": len(kept)}
        elif method == "prompt.submit":
            params["submission_id"] = uuid.uuid4().hex
            if self.images.get(sid):
                params["attachments"] = list(self.images[sid])
            result = await self.scoped(method, params)
            if result.get("status") not in ("queued", "started", "terminal") or not result.get(
                "admission_id"
            ):
                raise ValueError("Gateway did not confirm admission")
            self.images.pop(sid, None)
            self.admissions[sid] = dict(result)
            # Admission is durable and images are bound to that input, including while queued.
            return {**result, "status": "streaming"}
        elif method == "session.interrupt":
            own = self.admissions.get(sid)
            if own:
                receipt = await self.scoped(
                    "prompt.receipt", {"session_id": sid, "admission_id": own["admission_id"]}
                )
                if receipt.get("status") == "queued":
                    return await self.scoped(
                        "prompt.cancel", {"session_id": sid, "admission_id": own["admission_id"]}
                    )
                if receipt.get("status") == "terminal":
                    return {}
                if receipt.get("status") != "started" or type(receipt.get("execution_generation")) is not int:
                    raise ValueError("Uncertain turn execution; reconnect before stopping")
                params["execution_generation"] = receipt["execution_generation"]
                return await self.scoped(method, params)
            params["execution_generation"] = self.sessions[sid]["execution_generation"]
        elif method == "session.title" or (method == "config.set" and params.get("key") == "model"):
            rename = method == "session.title"
            operation = "rename" if rename else "model"
            payload = {"title": params["title"]} if rename else {"model": params["value"]}
            token = self.confirmations.get((sid, params.get("value")))
            if params.get("confirm_expensive_model") and token:
                payload["confirm"] = token
            result = await self.scoped(
                "session.mutate",
                {
                    "session_id": sid,
                    "request_id": uuid.uuid4().hex,
                    "expected_revision": self.sessions[sid]["revision"],
                    "operation": operation,
                    "payload": payload,
                },
            )
            if result.get("confirm_required"):
                self.confirmations[(sid, params.get("value"))] = result["confirm"]
            self.sessions[sid].update(
                {key: result[key] for key in ("revision", "execution_generation") if key in result}
            )
            return {
                **result,
                **({"title": params["title"]} if rename else {}),
                "deferred": result.get("status") == "deferred",
            }
        return await self.scoped(method, params)


async def serve(grant: dict) -> None:
    from websockets.asyncio.client import connect

    async with connect(
        grant["url"], subprotocols=grant["protocols"], proxy=None, max_size=32 << 20, open_timeout=10
    ) as ws:
        if ws.subprotocol != "hermes-gateway-v1":
            raise ValueError("Incompatible gateway protocol")
        pending = {}

        async def call(method, params):
            rid = "tern-" + uuid.uuid4().hex
            future = pending[rid] = asyncio.get_running_loop().create_future()
            try:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
                return await asyncio.wait_for(future, 30)
            finally:
                pending.pop(rid, None)

        adapter = Adapter(call)
        tasks = set()

        async def handle(frame):
            rid = frame.get("id")
            try:
                if "method" not in frame:
                    if not await adapter.answer(frame):
                        await ws.send(json.dumps(frame))
                    return
                result = await adapter.request(frame["method"], frame.get("params") or {})
                emit({"jsonrpc": "2.0", "id": rid, "result": result})
                if frame["method"] in ("session.create", "session.resume"):
                    sid = result["session_id"]
                    if result.get("running"):
                        emit(
                            {
                                "method": "event",
                                "params": {"session_id": sid, "type": "message.start", "payload": {}},
                            }
                        )
                    for prompt in result.get("prompts", []):
                        for event in adapter.event(
                            {
                                "method": "event",
                                "params": {
                                    "session_id": sid,
                                    "type": prompt["kind"] + ".request",
                                    "payload": prompt,
                                },
                            }
                        ):
                            emit(event)
            except Exception as exc:
                if "method" not in frame or not isinstance(exc, RpcError):
                    emit(
                        {
                            "local_error": "Gateway operation was not confirmed. Reconnect and review; nothing was resent."
                        }
                    )
                    await ws.close()
                else:
                    emit(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": getattr(exc, "code", -32000),
                                "message": str(exc),
                            },
                        }
                    )

        async def receive():
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    future = pending.get(frame.get("id"))
                    if future is not None:
                        if not future.done():
                            if "error" in frame:
                                future.set_exception(RpcError(frame["error"]))
                            else:
                                future.set_result(frame.get("result", {}))
                    elif "method" in frame:
                        for event in adapter.event(frame):
                            emit(event)
            finally:
                for future in list(pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError("Gateway disconnected"))

        reader = asyncio.create_task(receive())
        lines = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def read_stdin():
            while True:
                line = sys.stdin.readline()
                try:
                    loop.call_soon_threadsafe(lines.put_nowait, line)
                except RuntimeError:
                    return
                if not line:
                    return

        threading.Thread(target=read_stdin, daemon=True).start()
        try:
            while not reader.done():
                line_task = asyncio.create_task(lines.get())
                done, _ = await asyncio.wait((line_task, reader), return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    line_task.cancel()
                    emit(
                        {
                            "local_error": "Hermes gateway disconnected. Reconnect to review; work was not resent."
                        }
                    )
                    return
                line = line_task.result()
                if not line:
                    return
                task = asyncio.create_task(handle(json.loads(line)))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            reader.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(reader, *tasks, return_exceptions=True)


def main() -> int:
    original = sys.argv[1]
    try:
        grant = bootstrap(original)
        if grant is None:
            os.execv(original, [original, "--run-module", "tui_gateway.entry"])
        asyncio.run(serve(grant))
        return 0
    except Exception:
        emit(
            {
                "local_error": "Hermes gateway could not connect. Run Hermes doctor with the original launcher, then reconnect."
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
