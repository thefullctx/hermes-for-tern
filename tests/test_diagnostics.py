import json
import sys
from types import SimpleNamespace

from hermes_for_tern import diagnostics, launcher


def backend(tmp_path, body):
    path = tmp_path / "backend.py"
    path.write_text(body)
    return [sys.executable, "-u", str(path)]


def test_probe_exchanges_capabilities_without_creating_session(tmp_path):
    marker = tmp_path / "session-created"
    command = backend(
        tmp_path,
        f"""import json,sys
print(json.dumps({{"method":"event","params":{{"type":"gateway.ready"}}}}), flush=True)
for line in sys.stdin:
    f=json.loads(line)
    if f.get("method")=="session.create": Path({str(marker)!r}).touch()
    print(json.dumps({{"id":f["id"],"result":{{"server_requests":["approval","clarify"]}}}}), flush=True)
""",
    )
    lines = diagnostics.probe(command, tmp_path)
    assert any("OK backend startup" in line for line in lines)
    assert not marker.exists()


def test_probe_times_out_and_reaps_hung_backend(tmp_path):
    command = backend(tmp_path, "import time; time.sleep(30)")
    assert any("timed out" in line for line in diagnostics.probe(command, tmp_path, timeout=0.05))


def test_probe_redacts_arbitrary_backend_error(tmp_path):
    command = backend(
        tmp_path,
        """import json,sys
print('{"method":"event","params":{"type":"gateway.ready"}}', flush=True)
f=json.loads(sys.stdin.readline())
print(json.dumps({"id":f["id"],"error":{"code":1,"message":"SECRET-KEY do not print"}}), flush=True)
""",
    )
    lines = diagnostics.probe(command, tmp_path)
    assert any(line.startswith("FAIL") for line in lines)
    assert "SECRET-KEY" not in " ".join(lines)


def test_probe_identifies_incompatible_capability_response(tmp_path):
    command = backend(
        tmp_path,
        """import json,sys
print('{"method":"event","params":{"type":"gateway.ready"}}', flush=True)
f=json.loads(sys.stdin.readline())
print(json.dumps({"id":f["id"],"result":{}}), flush=True)
""",
    )
    assert any("incompatible" in line for line in diagnostics.probe(command, tmp_path))


def test_doctor_identifies_changed_launcher_and_missing_components(tmp_path, monkeypatch):
    target = tmp_path / "hermes"
    target.write_text("changed")
    original = tmp_path / "hermes.before-tern"
    original.write_text("original")
    monkeypatch.setattr(
        launcher, "settings", lambda: {"target": str(target), "original": str(original), "replaced": True}
    )
    monkeypatch.setattr(diagnostics, "version", lambda _: "1.2.3")
    monkeypatch.setattr(diagnostics, "probe", lambda *args: ["OK backend"])
    lines = diagnostics.diagnose(tmp_path, surface_caps=SimpleNamespace(kinds=set(), has=lambda _: False))
    assert any("launcher differs" in line for line in lines)
    assert any("lacks required" in line for line in lines)


def test_cli_doctor_returns_failure_for_broken_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = launcher.settings_path()
    path.parent.mkdir()
    path.write_text(json.dumps({"original": "/nonexistent"}))
    assert launcher.main(["--doctor"]) == 1
    assert "FAIL launcher/settings" in capsys.readouterr().out


def test_version_output_is_restricted_to_version_token(monkeypatch):
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Hermes 1.2.3 secret=DO-NOT-PRINT"),
    )
    assert diagnostics.version(["hermes"]) == "1.2.3"


def test_doctor_handles_settings_that_are_valid_json_but_not_an_object(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = launcher.settings_path()
    path.parent.mkdir()
    path.write_text("[]")
    assert launcher.main(["--doctor"]) == 1
    assert "FAIL launcher/settings" in capsys.readouterr().out
