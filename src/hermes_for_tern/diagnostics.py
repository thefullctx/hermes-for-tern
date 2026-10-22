"""Bounded, session-free checks. Never include backend stderr or arbitrary RPC errors."""

from __future__ import annotations

import importlib.metadata
import queue
import re
import subprocess
import time
from pathlib import Path

import tern_sdk

from .rpc import Backend

REQUIRED_KINDS = {"col", "card", "md", "editor", "tool", "el"}


def version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        # A version is a token, not arbitrary program output (which can contain credentials).
        match = re.search(r"\bv?\d+\.\d+\.\d+\b", result.stdout)
        return match.group(0)[:80] if result.returncode == 0 and match else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def probe(command: list[str], cwd: Path, timeout: float = 45) -> list[str]:
    backend = None
    deadline = time.monotonic() + timeout
    try:
        backend = Backend(command, cwd)
        while True:
            frame = backend.events.get(timeout=max(0.001, deadline - time.monotonic()))
            if "local_error" in frame:
                return ["FAIL backend disconnected during startup. Check Hermes installation and --backend."]
            params = frame.get("params") or {}
            if params.get("type") == "gateway.ready":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError
        result = backend.request("client.capabilities", {"server_requests": True}).result(
            timeout=max(0.001, deadline - time.monotonic())
        )
        if not isinstance(result, dict) or not isinstance(result.get("server_requests"), list):
            return [
                "FAIL backend capability response is incompatible. Update Hermes or select a tested backend."
            ]
        offered = set(result["server_requests"])
        missing = {"approval", "clarify"} - offered
        lines = ["OK backend startup and JSON-RPC capability exchange; no session created."]
        lines.append(
            "WARN backend does not advertise approval/clarification support. Use a compatible Hermes build."
            if missing
            else "OK backend advertises approval and clarification requests."
        )
        lines.append(
            "OK shared gateway protocol hermes-gateway-v1; closing this viewer leaves the owner running."
            if result.get("gateway_protocol") == "hermes-gateway-v1"
            else "Hermes does not advertise a gateway protocol version here; this checks the handshake, not every feature."
        )
        return lines
    except (TimeoutError, queue.Empty):
        return ["FAIL backend startup/handshake timed out. Check --backend and run Hermes doctor directly."]
    except Exception:
        return ["FAIL backend could not complete its handshake. Check Hermes installation and --backend."]
    finally:
        if backend:
            backend.close()


def diagnose(cwd: Path, *, surface_caps=None) -> list[str]:
    from . import launcher

    lines = [
        f"Adapter {importlib.metadata.version('hermes-for-tern')}; Tern SDK protocol {tern_sdk.wire.VERSION}."
    ]
    try:
        config = launcher.settings()
        original = launcher.original_command(config)
        target = config.get("target")
        if target:
            path = Path(target)
            if not launcher.launcher_matches(path):
                lines.append(
                    "FAIL managed launcher differs from this installation. Inspect it before reinstalling."
                )
            else:
                lines.append("OK managed launcher matches this adapter installation.")
            if config.get("replaced"):
                lines.append(
                    "OK original launcher backup exists."
                    if Path(config["original"]).is_file()
                    else "FAIL original launcher backup is missing; restore it before uninstalling."
                )
        else:
            lines.append("OK direct launch; no managed wrapper installed.")
        lines.append(
            f"Hermes version: {version([original, '--version'])}; Tern version: {version(['tern', '--version'])}."
        )
        if surface_caps:
            kinds = getattr(surface_caps, "kinds", set())
            has = getattr(surface_caps, "has", lambda _: False)
            lines.append(
                "OK Tern required components and dock."
                if REQUIRED_KINDS.issubset(kinds) and has("dock")
                else "FAIL Tern lacks required components or dock. Update Tern."
            )
        else:
            lines.append(
                "Tern surface capabilities must be checked inside Tern; --doctor does not open a surface."
            )
        lines.extend(probe(launcher.backend_command(original, config), cwd))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        lines.append(
            "FAIL launcher/settings are unavailable or invalid. Restore the original launcher or reinstall the wrapper."
        )
    return lines
