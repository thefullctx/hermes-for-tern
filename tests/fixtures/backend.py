"""Deterministic backend for native UI approval QA. No model, credentials or tools."""

import json
import sys


def send(frame):
    print(json.dumps({"jsonrpc": "2.0", **frame}), flush=True)


def event(kind, payload=None):
    send({"method": "event", "params": {"type": kind, "session_id": "qa", "payload": payload or {}}})


event("gateway.ready")
for line in sys.stdin:
    frame = json.loads(line)
    method, rid = frame.get("method"), frame.get("id")
    if method == "client.capabilities":
        send({"id": rid, "result": {"server_requests": ["approval"]}})
    elif method == "session.create":
        send(
            {
                "id": rid,
                "result": {
                    "session_id": "qa",
                    "stored_session_id": "qa",
                    "info": {"model": "UI test · no model calls"},
                },
            }
        )
    elif method == "prompt.submit":
        send({"id": rid, "result": {"status": "streaming"}})
        event("message.start")
        send(
            {
                "id": "srq-approval",
                "method": "approval",
                "params": {
                    "session_id": "qa",
                    "request_id": "approval-qa",
                    "choices": ["once", "deny"],
                    "description": "Controlled UI test. This does not execute a command.",
                    "command": "printf 'approval-ui-ok\\n'",
                },
            }
        )
    elif method == "approval.received":
        send({"id": rid, "result": {"acknowledged": True}})
    elif method == "session.interrupt":
        event("request.cancel", {"id": "srq-approval", "method": "approval", "reason": "interrupted"})
        event("message.complete", {"status": "interrupted"})
        send({"id": rid, "result": {"status": "interrupted"}})
    elif method == "session.close":
        send({"id": rid, "result": {"closed": True}})
    elif rid == "srq-approval":
        choice = frame.get("result", {}).get("choice", "deny")
        event("message.complete", {"status": "complete", "text": f"Approval answer received: {choice}."})
    else:
        send({"id": rid, "error": {"code": -32601, "message": "Unknown method"}})
