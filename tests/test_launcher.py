import json
import sys

import pytest

from hermes_for_tern import launcher


def test_supported_chat_options_and_original_cli_passthrough():
    assert launcher.native_options(["-m", "model", "-p", "work"]) == {"model": "model", "profile": "work"}
    for args in (
        ["model"],
        ["--help"],
        ["--version"],
        ["--run-module", "tui_gateway.entry"],
        ["--resume", "id"],
    ):
        assert launcher.native_options(args) is None


def test_install_uninstall_preserves_original_launcher_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "hermes"
    original = '#!/bin/sh\nexec real-hermes "$@"\n'
    target.write_text(original)
    target.chmod(0o755)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: str(target))
    launcher.install()
    saved = json.loads(launcher.settings_path().read_text())
    assert target.read_text().startswith("#!/bin/sh\n" + launcher.MARKER)
    assert sys.executable in target.read_text()
    assert launcher.original_command(saved) == saved["original"]
    launcher.uninstall()
    assert target.read_text() == original
    assert target.stat().st_mode & 0o777 == 0o755


def test_uninstall_refuses_to_overwrite_a_later_user_change(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "hermes"
    target.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: str(target))
    launcher.install()
    target.write_text("user changed this")
    with pytest.raises(RuntimeError, match="changed"):
        launcher.uninstall()
    assert target.read_text() == "user changed this"


def test_outside_tern_runs_original_interactive_command_with_same_options(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "settings", lambda: {})
    monkeypatch.setattr(launcher, "original_command", lambda _: "/real/hermes")
    monkeypatch.setattr(launcher.tern_sdk, "connect", lambda **_: None)
    monkeypatch.setattr(launcher, "passthrough", lambda cmd, args: calls.append((cmd, args)) or 0)
    assert launcher.main(["--model", "m", "--profile", "work"]) == 0
    assert calls == [("/real/hermes", ["--model", "m", "--profile", "work"])]


def test_subcommands_bypass_surface_detection(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "settings", lambda: {})
    monkeypatch.setattr(launcher, "original_command", lambda _: "/real/hermes")
    monkeypatch.setattr(
        launcher.tern_sdk, "connect", lambda **_: pytest.fail("Subcommands must not probe Tern")
    )
    monkeypatch.setattr(launcher, "passthrough", lambda cmd, args: calls.append((cmd, args)) or 0)
    assert launcher.main(["config", "get", "model"]) == 0
    assert calls == [("/real/hermes", ["config", "get", "model"])]


def test_visual_demo_defaults_to_five_minutes_and_never_falls_back_to_hermes(monkeypatch):
    assert launcher.native_options(["--demo"]) == {"demo": 300.0}
    assert launcher.native_options(["--demo", "20"]) == {"demo": 20.0}
    monkeypatch.setattr(launcher, "settings", lambda: {})
    monkeypatch.setattr(launcher, "original_command", lambda _: "/real/hermes")
    monkeypatch.setattr(launcher.tern_sdk, "connect", lambda **_: None)
    monkeypatch.setattr(launcher, "passthrough", lambda *_: pytest.fail("Demo must not run real Hermes"))
    assert launcher.main(["--demo"]) == 1
