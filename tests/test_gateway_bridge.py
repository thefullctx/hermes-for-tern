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
