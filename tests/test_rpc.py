import sys

from hermes_for_tern.rpc import Backend


def test_bidirectional_rpc_interleaves_notifications_requests_and_responses(tmp_path):
    server = tmp_path / "server.py"
    server.write_text("""import json,sys
def send(frame): print(json.dumps(frame), flush=True)
for line in sys.stdin:
    f=json.loads(line)
    if f.get("method")=="go":
        send({"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","payload":{"text":"Hi"}}})
        send({"jsonrpc":"2.0","id":"srq-1","method":"approval","params":{}})
        waiting=f["id"]
    elif f.get("id")=="srq-1":
        send({"jsonrpc":"2.0","id":waiting,"result":f["result"]})
""")
    backend = Backend([sys.executable, "-u", str(server)], tmp_path)
    try:
        result = backend.request("go")
        assert backend.events.get(timeout=3)["method"] == "event"
        assert backend.events.get(timeout=3)["id"] == "srq-1"
        backend.answer("srq-1", {"choice": "deny"})
        assert result.result(timeout=3) == {"choice": "deny"}
    finally:
        backend.close()
    assert backend.process.poll() == 0


def test_backend_crash_releases_pending_requests(tmp_path):
    backend = Backend([sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(7)"], tmp_path)
    future = backend.request("go")
    try:
        import pytest

        with pytest.raises(BrokenPipeError):
            future.result(timeout=3)
    finally:
        backend.close()
