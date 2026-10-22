"""Native QA backend. No model calls, credentials, shell commands, or project file changes."""

import base64
import hashlib
import json
import os
import sys


def send(frame):
    print(json.dumps({"jsonrpc": "2.0", **frame}), flush=True)


def event(kind, payload=None):
    send({"method": "event", "params": {"type": kind, "session_id": "qa", "payload": payload or {}}})


event("gateway.ready")
staged_images = []
for line in sys.stdin:
    frame = json.loads(line)
    method, rid = frame.get("method"), frame.get("id")
    if method == "client.capabilities":
        result = {"server_requests": ["approval", "clarify"]}
    elif method in ("session.create", "session.resume"):
        result = {
            "session_id": "qa",
            "stored_session_id": "qa",
            "session_key": "qa",
            "info": {"model": "Native QA · controlled backend"},
        }
        if method == "session.resume":
            result["messages"] = [
                {"role": "user", "text": "Earlier QA conversation"},
                {"role": "assistant", "text": "Connection restored from stored session."},
            ]
    elif method == "session.list":
        result = {"sessions": []}
    elif method == "image.attach_bytes":
        params = frame["params"]
        digest = hashlib.sha256(base64.b64decode(params["content_base64"], validate=True)).hexdigest()
        path = f"/qa/{digest}"
        staged_images.append((path, digest[:8]))
        result = {"attached": True, "path": path, "count": len(staged_images)}
    elif method == "image.detach":
        path = frame["params"]["path"]
        before = len(staged_images)
        staged_images = [image for image in staged_images if image[0] != path]
        result = {"detached": len(staged_images) < before, "count": len(staged_images)}
    elif method == "prompt.submit":
        text = frame.get("params", {}).get("text", "")
        images, staged_images = staged_images, []
        image_notice = f" · images={len(images)} · {','.join(image[1] for image in images)}" if images else ""
        send({"id": rid, "result": {"status": "streaming"}})
        event("message.start")
        if text == "crash":
            event(
                "tool.start",
                {
                    "tool_id": "qa-tool",
                    "name": "terminal",
                    "args": {"command": "SIMULATED · no commands run"},
                },
            )
            os._exit(7)
        if text == "approval":
            send(
                {
                    "id": "qa-approval",
                    "method": "approval",
                    "params": {
                        "session_id": "qa",
                        "request_id": "qa-approval",
                        "choices": ["once", "deny"],
                        "description": "Controlled QA; no commands will execute.",
                    },
                }
            )
        elif text == "clarify":
            send(
                {
                    "id": "qa-question",
                    "method": "clarify",
                    "params": {
                        "session_id": "qa",
                        "questions": [
                            {
                                "qid": "choice",
                                "question": "Choose a test answer",
                                "choices": ["Alpha", "Beta"],
                            }
                        ],
                    },
                }
            )
        elif text == "wait":
            event("status.update", {"text": "Waiting for Stop · controlled QA"})
        else:
            event("tool.start", {"tool_id": "delegate", "name": "delegate_task", "args": {}})
            event("subagent.start", {"subagent_id": "child", "goal": "Controlled child", "task_index": 0})
            event(
                "subagent.complete",
                {
                    "subagent_id": "child",
                    "status": "completed",
                    "summary": "Controlled delegation completed.",
                },
            )
            event("tool.complete", {"tool_id": "delegate", "result": {"output": "Native QA completed"}})
            event("message.complete", {"status": "complete", "text": f"Received: {text}{image_notice}"})
        continue
    elif method == "session.interrupt":
        event("message.complete", {"status": "interrupted"})
        result = {"status": "interrupted"}
    elif method in ("approval.received", "session.close"):
        result = {}
    elif rid in ("qa-approval", "qa-question"):
        event("message.complete", {"status": "complete", "text": "Native answer received."})
        continue
    else:
        send({"id": rid, "error": {"code": -32601, "message": "Unsupported QA method"}})
        continue
    send({"id": rid, "result": result})
