import asyncio
import json
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

from hermes_for_tern import gateway_bridge, launcher
from hermes_for_tern.gateway_bridge import Adapter
from hermes_for_tern.rpc import RpcError
from hermes_for_tern.rpc import Backend


SNAPSHOT = {
    "session_id": "local-one",
    "stored_session_id": "local-one",
    "subscription_id": "sub-one",
    "revision": 4,
    "execution_generation": 2,
    "messages": [],
    "info": {},
}


class Wire:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def run(coro):
    return asyncio.run(coro)


def test_default_launch_uses_shared_gateway_client_and_explicit_backend_is_preserved():
    command = launcher.backend_command("/original/hermes", {})
    assert command[-2:] == ["hermes_for_tern.gateway_bridge", "/original/hermes"]
    assert launcher.backend_command("/original/hermes", {"backend": ["custom", "--stdio"]}) == [
        "custom",
        "--stdio",
    ]


def test_new_session_drops_rendering_options_and_title_identity_but_preserves_policy():
    wire = Wire([SNAPSHOT, {**SNAPSHOT, "session_id": "local-two"}])
    adapter = Adapter(wire)
    params = {
        "cols": 100,
        "title": "Hermes for Tern",
        "source": "tui",
        "cwd": "/project",
        "cwd_explicit": True,
        "model": "chosen",
        "profile": "work",
    }
    run(adapter.request("session.create", params))
    run(adapter.request("session.create", params))
    for method, sent in wire.calls:
        assert method == "session.create" and "cols" not in sent and "title" not in sent
        assert sent["model"] == "chosen" and sent["profile"] == "work" and sent["cwd"] == "/project"
    assert wire.calls[0][1]["request_id"] != wire.calls[1][1]["request_id"]
    assert params["title"] == "Hermes for Tern"


def test_resume_projects_stored_history_and_only_falls_back_to_title_on_not_found():
    wire = Wire(
        [
            RpcError({"code": 4001, "message": "not_found"}),
            {
                **SNAPSHOT,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url"}]}
                ],
            },
        ]
    )
    result = run(Adapter(wire).request("session.resume", {"session_id": "named chat", "cols": 80}))
    assert result["messages"][0]["text"] == "look\n[image]"
    assert wire.calls[0][1] == {"session_id": "named chat", "display_projection": True}
    assert wire.calls[1][1] == {"title": "named chat", "display_projection": True}
    wire = Wire([RpcError({"message": "permission_denied"})])
    with pytest.raises(RpcError):
        run(Adapter(wire).request("session.resume", {"session_id": "other"}))
    assert len(wire.calls) == 1


def test_close_detaches_only_this_view_and_keeps_profile_binding():
    wire = Wire([{**SNAPSHOT}, {"detached": True}])
    adapter = Adapter(wire)
    run(adapter.request("session.create", {"profile": "work"}))
    run(adapter.request("session.close", {"session_id": "local-one"}))
    assert wire.calls[-1] == (
        "session.detach",
        {"session_id": "local-one", "subscription_id": "sub-one", "profile": "work"},
    )
    assert not adapter.sessions
    assert run(adapter.request("session.close", {"session_id": "unknown"})) == {}
    assert all(
        method not in ("session.close", "session.interrupt", "gateway.stop") for method, _ in wire.calls
    )


def test_images_bind_to_exact_admission_and_do_not_leak_into_next_prompt():
    wire = Wire(
        [
            {"attached": True, "path": "/cache/a.png", "mime": "image/png", "count": 1},
            {"attached": True, "path": "/cache/b.jpg", "mime": "image/jpeg", "count": 1},
            {"status": "queued", "admission_id": "admission-a"},
            {"status": "queued", "admission_id": "admission-b"},
        ]
    )
    adapter = Adapter(wire)
    assert (
        run(adapter.request("image.detach", {"session_id": "s", "path": "/nonexistent/probe"}))["count"] == 0
    )
    for count in (1, 2):
        assert (
            run(adapter.request("image.attach_bytes", {"session_id": "s", "content_base64": "fixture"}))[
                "count"
            ]
            == count
        )
    result = run(adapter.request("prompt.submit", {"session_id": "s", "text": "first"}))
    assert result["status"] == "streaming" and result["admission_id"] == "admission-a"
    assert wire.calls[-1][1]["attachments"] == [
        {"path": "/cache/a.png", "mime": "image/png"},
        {"path": "/cache/b.jpg", "mime": "image/jpeg"},
    ]
    identity = wire.calls[-1][1]["submission_id"]
    run(adapter.request("prompt.submit", {"session_id": "s", "text": "second"}))
    assert "attachments" not in wire.calls[-1][1] and wire.calls[-1][1]["submission_id"] != identity


def test_rejected_image_admission_can_roll_back_without_deleting_gateway_files():
    wire = Wire(
        [
            {"attached": True, "path": "/cache/a.png", "mime": "image/png"},
            RpcError({"message": "invalid_params"}),
        ]
    )
    adapter = Adapter(wire)
    run(adapter.request("image.attach_bytes", {"session_id": "s"}))
    with pytest.raises(RpcError):
        run(adapter.request("prompt.submit", {"session_id": "s", "text": "first"}))
    assert run(adapter.request("image.detach", {"session_id": "s", "path": "/cache/a.png"})) == {
        "detached": True,
        "count": 0,
    }
    assert len(wire.calls) == 2


@pytest.mark.parametrize("status", ["queued", "started"])
def test_stop_targets_this_admission_even_when_its_execution_started_after_the_ack(status):
    wire = Wire(
        [
            {"status": "queued", "admission_id": "own"},
            {"status": status, "admission_id": "own", "execution_generation": 9},
            {},
        ]
    )
    adapter = Adapter(wire)
    run(adapter.request("prompt.submit", {"session_id": "s", "text": "test"}))
    run(adapter.request("session.interrupt", {"session_id": "s"}))
    assert wire.calls[-1] == (
        ("prompt.cancel", {"session_id": "s", "admission_id": "own"})
        if status == "queued"
        else ("session.interrupt", {"session_id": "s", "execution_generation": 9})
    )


def test_other_viewers_completion_cannot_finish_our_queued_turn_and_cancel_is_interrupted():
    adapter = Adapter(Wire([{"status": "queued", "admission_id": "own"}]))
    run(adapter.request("prompt.submit", {"session_id": "s", "text": "test"}))
    other = {
        "params": {
            "type": "message.complete",
            "session_id": "s",
            "admission_id": "other",
            "payload": {"status": "complete"},
        }
    }
    assert adapter.event(other) == [] and "s" in adapter.admissions
    cancelled = {
        "params": {
            "type": "message.complete",
            "session_id": "s",
            "admission_id": "own",
            "payload": {"outcome": "cancelled"},
        }
    }
    assert adapter.event(cancelled)[0]["params"]["payload"]["status"] == "interrupted"
    assert not adapter.admissions


@pytest.mark.parametrize("reply", [{}, {"status": "queued"}, {"status": "unknown", "admission_id": "a"}])
def test_unconfirmed_admission_is_never_retried(reply):
    wire = Wire([reply])
    with pytest.raises(ValueError, match="admission"):
        run(Adapter(wire).request("prompt.submit", {"session_id": "s", "text": "first"}))
    assert len(wire.calls) == 1


@pytest.mark.parametrize("kind", ["approval", "clarify"])
def test_shared_prompt_response_is_bound_to_its_original_session_and_generation(kind):
    wire = Wire([{}])
    adapter = Adapter(wire)
    prompt = {"prompt_id": "p", "execution_generation": 3, "choices": ["once", "deny"], "question": "Pick?"}
    frame = adapter.event(
        {"method": "event", "params": {"session_id": "s", "type": kind + ".request", "payload": prompt}}
    )[0]
    assert frame["method"] == kind and frame["params"]["session_id"] == "s"
    result = {"choice": "once"} if kind == "approval" else {"answers": {"p": "answer"}}
    assert run(adapter.answer({"id": frame["id"], "result": result}))
    method, sent = wire.calls[-1]
    assert method == kind + ".respond" and sent["execution_generation"] == 3 and sent["session_id"] == "s"
    assert sent["choice" if kind == "approval" else "answer"] == ("once" if kind == "approval" else "answer")


def test_settled_prompt_and_replay_gap_do_not_leave_stale_controls_or_hide_missing_events():
    adapter = Adapter(Wire([]))
    prompt = {"prompt_id": "p", "execution_generation": 3, "choices": ["deny"]}
    frame = adapter.event({"params": {"session_id": "s", "type": "approval.request", "payload": prompt}})[0]
    cancel = adapter.event({"params": {"session_id": "s", "type": "approval.settled", "payload": prompt}})[0]
    assert cancel["params"]["payload"]["id"] == frame["id"] and not adapter.controls
    with pytest.raises(RuntimeError, match="Reconnect"):
        adapter.event({"params": {"type": "session.replay_gap"}})


def test_an_unrelated_view_does_not_approve_or_deny_another_sessions_card():
    wire = Wire([])
    adapter = Adapter(wire)
    prompt = {"prompt_id": "p", "execution_generation": 3, "choices": ["once", "deny"]}
    frame = adapter.event({"params": {"session_id": "other", "type": "approval.request", "payload": prompt}})[
        0
    ]
    assert run(adapter.answer({"id": frame["id"], "error": {"message": "Session is not displayed here"}}))
    assert not wire.calls


def test_interrupt_uses_current_execution_stamp_and_model_confirmation_uses_owner_token():
    wire = Wire([SNAPSHOT, {}, {"confirm_required": True, "confirm": "owner-token"}, {"revision": 5}])
    adapter = Adapter(wire)
    run(adapter.request("session.create", {}))
    adapter.event(
        {
            "params": {
                "session_id": "local-one",
                "type": "message.start",
                "execution_generation": 7,
                "payload": {},
            }
        }
    )
    run(adapter.request("session.interrupt", {"session_id": "local-one"}))
    assert wire.calls[-1][1]["execution_generation"] == 7
    run(adapter.request("config.set", {"session_id": "local-one", "key": "model", "value": "model-a"}))
    run(
        adapter.request(
            "config.set",
            {"session_id": "local-one", "key": "model", "value": "model-a", "confirm_expensive_model": True},
        )
    )
    assert wire.calls[-1][0] == "session.mutate"
    assert wire.calls[-1][1]["expected_revision"] == 4
    assert wire.calls[-1][1]["payload"] == {"model": "model-a", "confirm": "owner-token"}


def test_bootstrap_grant_stays_private_and_legacy_fallback_is_only_for_missing_helper(monkeypatch):
    grant = {
        "url": "ws://127.0.0.1:1234/api/ws",
        "protocols": ["hermes-gateway-v1", "hermes-gateway-ticket.PRIVATE"],
    }
    monkeypatch.setattr(
        gateway_bridge.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(grant)),
    )
    assert gateway_bridge.bootstrap("/original") == grant
    monkeypatch.setattr(
        gateway_bridge.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="No module named 'ui-tui'"),
    )
    assert gateway_bridge.bootstrap("/original") is None
    monkeypatch.setattr(
        gateway_bridge.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="No module named 'broken_dependency'"),
    )
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        gateway_bridge.bootstrap("/original")
    grant["url"] = "ws://192.0.2.1:1234/api/ws"
    monkeypatch.setattr(
        gateway_bridge.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(grant)),
    )
    with pytest.raises(ValueError):
        gateway_bridge.bootstrap("/original")


@pytest.mark.parametrize("disconnect", [False, True])
def test_real_bridge_socket_transport_controls_and_shutdown_preserve_owner(tmp_path, disconnect):
    from websockets.sync.client import connect
    from websockets.sync.server import serve

    calls = queue.Queue()

    def gateway(ws):
        ws.send(
            json.dumps(
                {
                    "method": "event",
                    "params": {"type": "gateway.ready", "payload": {"session_authority": True}},
                }
            )
        )
        for raw in ws:
            frame = json.loads(raw)
            method, params = frame["method"], frame.get("params") or {}
            calls.put((method, params))
            if method == "client.capabilities":
                result = {"server_requests": ["approval", "clarify"]}
            elif method == "session.create":
                result = SNAPSHOT
            elif method == "prompt.submit":
                if disconnect:
                    ws.close()
                    return
                result = {"status": "queued", "admission_id": "a"}
            else:
                result = {"detached": True}
            ws.send(json.dumps({"id": frame["id"], "result": result}))
            if method == "prompt.submit":
                ws.send(
                    json.dumps(
                        {
                            "method": "event",
                            "params": {
                                "type": "approval.request",
                                "session_id": "local-one",
                                "payload": {
                                    "prompt_id": "p",
                                    "execution_generation": 2,
                                    "choices": ["once", "deny"],
                                    "description": "controlled test",
                                },
                            },
                        }
                    )
                )
            elif method == "approval.respond":
                ws.send(
                    json.dumps(
                        {
                            "method": "event",
                            "params": {
                                "type": "message.complete",
                                "session_id": "local-one",
                                "payload": {"status": "complete", "text": "controlled answer"},
                            },
                        }
                    )
                )

    with serve(gateway, "127.0.0.1", 0, subprotocols=["hermes-gateway-v1"]) as server:
        port = server.socket.getsockname()[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = tmp_path / "fake-hermes"
        grant = {
            "url": f"ws://127.0.0.1:{port}/api/ws",
            "protocols": ["hermes-gateway-v1", "hermes-gateway-ticket.FIXTURE_PRIVATE"],
        }
        original.write_text(f"#!{sys.executable}\nprint({json.dumps(grant)!r})\n")
        original.chmod(0o700)
        backend = Backend(launcher.backend_command(str(original), {}), tmp_path)
        try:
            assert backend.events.get(timeout=5)["params"]["type"] == "gateway.ready"
            assert backend.request("client.capabilities", {"server_requests": True}).result(timeout=5)[
                "server_requests"
            ] == ["approval", "clarify"]
            assert (
                backend.request("session.create", {"cols": 80, "title": "Hermes for Tern"}).result(timeout=5)[
                    "session_id"
                ]
                == "local-one"
            )
            prompt = backend.request("prompt.submit", {"session_id": "local-one", "text": "fixture"})
            if disconnect:
                with pytest.raises(Exception):
                    prompt.result(timeout=5)
                while "local_error" not in backend.events.get(timeout=5):
                    pass
                # No executor thread can pin the client waiting for still-open stdin.
                backend.process.wait(timeout=5)
            else:
                assert prompt.result(timeout=5)["status"] == "streaming"
                approval = backend.events.get(timeout=5)
                assert approval["method"] == "approval" and approval["params"]["choices"] == ["once", "deny"]
                backend.answer(approval["id"], {"choice": "deny"})
                assert backend.events.get(timeout=5)["params"]["type"] == "message.complete"
            assert "FIXTURE_PRIVATE" not in "\n".join(backend.logs)
        finally:
            backend.close("local-one")
            try:
                # The owner still accepts another viewer after this client's close.
                with connect(grant["url"], subprotocols=["hermes-gateway-v1"], proxy=None) as second:
                    assert json.loads(second.recv(timeout=2))["params"]["type"] == "gateway.ready"
            finally:
                server.shutdown()
                thread.join(timeout=3)
        sent = list(calls.queue)
        assert not any(method in ("session.close", "gateway.stop") for method, _ in sent)
        if not disconnect:
            assert (
                "approval.respond",
                {"session_id": "local-one", "execution_generation": 2, "prompt_id": "p", "choice": "deny"},
            ) in sent
            assert any(method == "session.detach" for method, _ in sent)


class BridgeSocket:
    subprotocol = "hermes-gateway-v1"

    def __init__(self, reply):
        self.reply = reply
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.wait_limits = []

    async def send(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        result = self.reply(frame)
        if result is not None:
            await self.incoming.put(json.dumps({"id": frame["id"], **result}))

    async def close(self):
        self.closed = True
        await self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return raw


async def bridge_scenario(monkeypatch, reply, scenario):
    from contextlib import asynccontextmanager

    from websockets.asyncio import client

    socket = BridgeSocket(reply)
    stdin = queue.Queue()
    output = []

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield socket

    wait_for = asyncio.wait_for

    async def bounded_wait(future, timeout):
        socket.wait_limits.append(timeout)
        return await wait_for(future, min(timeout, 0.05))

    monkeypatch.setattr(client, "connect", connect)
    monkeypatch.setattr(gateway_bridge.sys, "stdin", SimpleNamespace(readline=stdin.get))
    monkeypatch.setattr(gateway_bridge, "emit", output.append)
    monkeypatch.setattr(gateway_bridge.asyncio, "wait_for", bounded_wait)
    task = asyncio.create_task(gateway_bridge.serve({"url": "ws://fixture", "protocols": []}))

    async def until(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    def send(frame):
        stdin.put(json.dumps(frame) + "\n")

    try:
        await scenario(socket, send, output, until)
    finally:
        stdin.put("")
        async with asyncio.timeout(3):
            await task


def test_sent_prompt_timeout_disconnects_without_retry(monkeypatch):
    async def scenario(socket, send, output, until):
        send({"id": 1, "method": "prompt.submit", "params": {"session_id": "s", "text": "once"}})
        await until(lambda: socket.closed)
        assert any("operation was not confirmed" in row.get("local_error", "") for row in output)
        assert [frame["method"] for frame in socket.sent] == ["prompt.submit"]

    run(bridge_scenario(monkeypatch, lambda frame: None, scenario))


@pytest.mark.parametrize("method", sorted(gateway_bridge.READ_ONLY_METHODS))
def test_read_only_timeout_fails_only_its_request_and_next_request_works(monkeypatch, method):
    async def scenario(socket, send, output, until):
        send({"id": 1, "method": method})
        await until(lambda: any(row.get("id") == 1 for row in output))
        assert output[-1]["error"] == {
            "code": -32001,
            "message": f"Hermes did not answer {method} in time",
        }
        assert not socket.closed and not any("local_error" in row for row in output)
        send({"id": 2, "method": "client.capabilities"})
        await until(lambda: any(row.get("id") == 2 for row in output))
        assert output[-1]["result"]["gateway_protocol"] == "hermes-gateway-v1"

    count = 0

    def reply(frame):
        nonlocal count
        count += 1
        return None if count == 1 else {"result": {}}

    run(bridge_scenario(monkeypatch, reply, scenario))


def test_missing_revision_fails_before_send_even_during_another_inflight_request(monkeypatch):
    async def scenario(socket, send, output, until):
        send({"id": 1, "method": "session.resume", "params": {"session_id": "s"}})
        await until(lambda: any(row.get("id") == 1 for row in output))
        send({"id": 2, "method": "session.list"})
        await until(lambda: len(socket.sent) == 2)
        send({"id": 3, "method": "session.title", "params": {"session_id": "s", "title": "new"}})
        await until(lambda: any(row.get("id") == 3 for row in output))
        error = next(row["error"] for row in output if row.get("id") == 3)
        assert "Missing revision" in error["message"]
        assert not socket.closed and not any("local_error" in row for row in output)
        assert [frame["method"] for frame in socket.sent] == ["session.resume", "session.list"]

    run(
        bridge_scenario(
            monkeypatch,
            lambda f: {"result": {"session_id": "s"}} if f["method"] == "session.resume" else None,
            scenario,
        )
    )


@pytest.mark.parametrize("method", ["prompt.submit", "slash.exec", "command.dispatch", "image.attach_bytes"])
@pytest.mark.parametrize(
    "reply",
    [
        None,
        {"result": []},
        {"result": None},
        {"error": {}},
        {"result": {}, "error": {"code": 4001, "message": "refused"}},
    ],
)
def test_uncertain_mutations_and_commands_disconnect_without_retry(monkeypatch, method, reply):
    async def scenario(socket, send, output, until):
        send({"id": 1, "method": method, "params": {"session_id": "s"}})
        await until(lambda: socket.closed)
        assert len(socket.sent) == 1
        assert len([row for row in output if "local_error" in row]) == 1
        assert not any(row.get("id") == 1 and "result" in row for row in output)

    run(bridge_scenario(monkeypatch, lambda frame: reply, scenario))


@pytest.mark.parametrize("kind", ["approval", "clarify"])
@pytest.mark.parametrize("refused", [True, False])
def test_control_refusal_cancels_card_but_timeout_still_disconnects(monkeypatch, kind, refused):
    async def scenario(socket, send, output, until):
        await socket.incoming.put(
            json.dumps(
                {
                    "method": "event",
                    "params": {
                        "session_id": "s",
                        "type": kind + ".request",
                        "payload": {"prompt_id": "p", "execution_generation": 3},
                    },
                }
            )
        )
        await until(lambda: bool(output))
        rid = output[0]["id"]
        send({"id": rid, "result": {"choice": "deny", "answers": {"p": "yes"}}})
        if not refused:
            await until(lambda: socket.closed)
            assert any("local_error" in row for row in output)
            return
        await until(lambda: any(row.get("params", {}).get("type") == "notice" for row in output))
        cancel = next(row for row in output if row.get("params", {}).get("type") == "request.cancel")
        assert cancel["params"]["payload"]["id"] == rid
        assert not socket.closed and not any("local_error" in row for row in output)
        # The refused control is removed; it cannot send another .respond call.
        send({"id": rid, "result": {"choice": "deny"}})
        await until(lambda: len(socket.sent) == 2)
        assert socket.sent[1].get("method") is None
        send({"id": 2, "method": "client.capabilities"})
        await until(lambda: any(row.get("id") == 2 for row in output))

    def reply(frame):
        if frame.get("method") == kind + ".respond":
            return {"error": {"code": 4001, "message": "already_settled"}} if refused else None
        return {"result": {}}

    run(bridge_scenario(monkeypatch, reply, scenario))


def test_replay_gap_reports_its_specific_message_only(monkeypatch):
    async def scenario(socket, send, output, until):
        await socket.incoming.put(json.dumps({"method": "event", "params": {"type": "session.replay_gap"}}))
        await until(lambda: any("local_error" in row for row in output))
        assert output == [
            {
                "local_error": "Gateway event history changed. Reconnect to review the session; nothing will be resent."
            }
        ]

    run(bridge_scenario(monkeypatch, lambda frame: {"result": {}}, scenario))


def test_malformed_events_and_json_are_visible_and_do_not_stop_later_events(monkeypatch):
    async def scenario(socket, send, output, until):
        malformed = [
            "not JSON",
            "[]",
            json.dumps({"method": "event", "params": []}),
            json.dumps(
                {"method": "event", "params": {"type": "approval.request", "session_id": "s", "payload": {}}}
            ),
            json.dumps(
                {
                    "method": "event",
                    "params": {"type": "clarify.request", "session_id": "s", "payload": {"prompt_id": "p"}},
                }
            ),
        ]
        for raw in malformed:
            await socket.incoming.put(raw)
        event = {
            "method": "event",
            "params": {"type": "message.delta", "session_id": "s", "payload": {"text": "still connected"}},
        }
        await socket.incoming.put(json.dumps(event))
        await until(lambda: event in output)
        notices = [row for row in output if row.get("params", {}).get("type") == "notice"]
        assert len(notices) == len(malformed)
        assert all("hermes --plain" in row["params"]["payload"]["message"] for row in notices[-2:])
        assert not socket.closed

    run(bridge_scenario(monkeypatch, lambda frame: {"result": {}}, scenario))


def test_read_only_key_error_fails_only_the_request(monkeypatch):
    original = Adapter.request

    async def request(self, method, params):
        if method == "session.list":
            raise KeyError("sessions")
        return await original(self, method, params)

    monkeypatch.setattr(Adapter, "request", request)

    async def scenario(socket, send, output, until):
        send({"id": 1, "method": "session.list"})
        await until(lambda: bool(output))
        assert "session.list failed" in output[0]["error"]["message"]
        send({"id": 2, "method": "client.capabilities"})
        await until(lambda: any(row.get("id") == 2 for row in output))
        assert not socket.closed and not any("local_error" in row for row in output)

    run(bridge_scenario(monkeypatch, lambda frame: {"result": {}}, scenario))


def test_missing_answer_generation_is_rejected_before_calling_wire():
    wire = Wire([])
    adapter = Adapter(wire)
    adapter.controls["control"] = ("approval", "s", {"prompt_id": "p"})
    with pytest.raises(ValueError, match="Missing execution_generation"):
        run(adapter.answer({"id": "control", "result": {"choice": "once"}}))
    assert not wire.calls


def test_real_socket_request_refusal_keeps_bridge_available(tmp_path):
    from websockets.sync.server import serve

    calls = []

    def gateway(ws):
        for raw in ws:
            frame = json.loads(raw)
            calls.append(frame["method"])
            response = (
                {"error": {"code": 4001, "message": "listing_unavailable"}}
                if frame["method"] == "session.list"
                else {"result": {"server_requests": ["approval", "clarify"]}}
            )
            ws.send(json.dumps({"id": frame["id"], **response}))

    with serve(gateway, "127.0.0.1", 0, subprotocols=["hermes-gateway-v1"]) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        grant = {
            "url": f"ws://127.0.0.1:{server.socket.getsockname()[1]}/api/ws",
            "protocols": ["hermes-gateway-v1"],
        }
        original = tmp_path / "fake-hermes"
        original.write_text(f"#!{sys.executable}\nprint({json.dumps(grant)!r})\n")
        original.chmod(0o700)
        backend = Backend(launcher.backend_command(str(original), {}), tmp_path)
        try:
            with pytest.raises(RpcError, match="listing_unavailable"):
                backend.request("session.list", {}).result(timeout=5)
            assert backend.request("client.capabilities", {}).result(timeout=5)["server_requests"] == [
                "approval",
                "clarify",
            ]
            assert backend.process.poll() is None
            assert backend.events.empty()
            assert calls == ["session.list", "client.capabilities"]
        finally:
            backend.close(None)
            server.shutdown()
            thread.join(timeout=3)


def test_only_large_listing_and_explicit_model_refresh_get_longer_deadlines(monkeypatch):
    async def scenario(socket, send, output, until):
        requests = [
            ("session.list", {}),
            ("model.options", {"refresh": True}),
            ("model.options", {}),
            ("model.options", {"refresh": False}),
            ("client.capabilities", {}),
        ]
        for rid, (method, params) in enumerate(requests, 1):
            send({"id": rid, "method": method, "params": params})
            await until(lambda: any(row.get("id") == rid for row in output))
        assert gateway_bridge.CALL_TIMEOUT == 30
        assert socket.wait_limits == [90, 90, 30, 30, 30]

    run(bridge_scenario(monkeypatch, lambda frame: {"result": {}}, scenario))


def test_malformed_read_only_acknowledgement_fails_only_its_request(monkeypatch):
    async def scenario(socket, send, output, until):
        send({"id": 1, "method": "session.list"})
        await until(lambda: bool(output))
        assert "Malformed gateway acknowledgement" in output[0]["error"]["message"]
        send({"id": 2, "method": "client.capabilities"})
        await until(lambda: any(row.get("id") == 2 for row in output))
        assert not socket.closed and not any("local_error" in row for row in output)

    run(
        bridge_scenario(
            monkeypatch,
            lambda f: {"error": {}} if f["method"] == "session.list" else {"result": {}},
            scenario,
        )
    )


@pytest.mark.parametrize("kind", ["approval", "clarify"])
def test_an_answer_settled_elsewhere_says_so_instead_of_vanishing_as_applied(kind):
    prompt = {"prompt_id": "p", "execution_generation": 3, "choices": ["once", "deny"], "question": "Pick?"}
    result = {"choice": "deny"} if kind == "approval" else {"answers": {"p": "no"}}
    wire = Wire([{"status": "already_resolved", "prompt_id": "p"}])
    adapter = Adapter(wire)
    frame = adapter.event({"params": {"session_id": "s", "type": kind + ".request", "payload": prompt}})[0]
    assert run(adapter.answer({"id": frame["id"], "result": result}))
    assert not adapter.controls
    [shown] = adapter.notices
    noun = "approval" if kind == "approval" else "question"
    assert shown["params"] == {
        "session_id": "s",
        "type": "notice",
        "payload": {
            "message": f"That {noun} was already answered in another window, so your answer wasn't applied."
        },
    }


@pytest.mark.parametrize("reply", [{"status": "resolved", "prompt_id": "p"}, {}])
def test_an_applied_answer_or_an_older_gateway_shows_no_note(reply):
    adapter = Adapter(Wire([reply]))
    prompt = {"prompt_id": "p", "execution_generation": 3, "choices": ["once", "deny"]}
    frame = adapter.event({"params": {"session_id": "s", "type": "approval.request", "payload": prompt}})[0]
    assert run(adapter.answer({"id": frame["id"], "result": {"choice": "once"}}))
    assert not adapter.controls and adapter.notices == []


@pytest.mark.parametrize("kind", ["approval", "clarify"])
def test_the_bridge_shows_when_an_answer_was_settled_elsewhere_and_stays_connected(monkeypatch, kind):
    async def scenario(socket, send, output, until):
        await socket.incoming.put(
            json.dumps(
                {
                    "method": "event",
                    "params": {
                        "session_id": "s",
                        "type": kind + ".request",
                        "payload": {"prompt_id": "p", "execution_generation": 3},
                    },
                }
            )
        )
        await until(lambda: bool(output))
        send({"id": output[0]["id"], "result": {"choice": "deny", "answers": {"p": "yes"}}})
        await until(lambda: any(row.get("params", {}).get("type") == "notice" for row in output))
        shown = next(row for row in output if row.get("params", {}).get("type") == "notice")
        assert "already answered in another window" in shown["params"]["payload"]["message"]
        assert not socket.closed and not any("local_error" in row for row in output)
        send({"id": 2, "method": "client.capabilities"})
        await until(lambda: any(row.get("id") == 2 for row in output))

    def reply(frame):
        if frame.get("method") == kind + ".respond":
            return {"result": {"status": "already_resolved", "prompt_id": "p"}}
        return {"result": {}}

    run(bridge_scenario(monkeypatch, reply, scenario))
