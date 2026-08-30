"""Minimal, credential-isolated Codex CLI wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .review import REVIEW_JSON_SCHEMA, Review
from .gitnexus import executable_command, mcp_config

SAFE_ENVIRONMENT_NAMES = {
    "APPDATA",
    "CODEX_HOME",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
REQUIRED_REVIEW_GITNEXUS_TOOLS = ("detect_changes", "context", "impact")


def safe_codex_environment() -> dict[str, str]:
    allowed = {name.upper() for name in SAFE_ENVIRONMENT_NAMES}
    return {name: value for name, value in os.environ.items() if name.upper() in allowed}


def assert_logged_in(codex_binary: str = "codex") -> None:
    try:
        result = subprocess.run(
            [*executable_command(codex_binary), "login", "status"],
            check=False,
            capture_output=True,
            text=True,
            env=safe_codex_environment(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Codex CLI was not found: {codex_binary}") from exc
    if result.returncode != 0:
        raise RuntimeError("Codex is not logged in; run 'codex login' on this trusted runner")


def run_codex_review(
    prompt: str,
    repository: Path,
    codex_binary: str = "codex",
    temp_root: Path | None = None,
    fixed_fields: dict[str, Any] | None = None,
    reasoning_effort: str = "medium",
    gitnexus_binary: str = "gitnexus",
) -> Review:
    raw = run_codex_json(
        prompt, REVIEW_JSON_SCHEMA, repository, codex_binary, temp_root,
        fixed_fields, reasoning_effort, gitnexus_binary,
        required_gitnexus_tools=REQUIRED_REVIEW_GITNEXUS_TOOLS,
    )
    return Review.from_json(raw)


def run_codex_json(
    prompt: str,
    schema: dict[str, Any],
    repository: Path,
    codex_binary: str = "codex",
    temp_root: Path | None = None,
    fixed_fields: dict[str, Any] | None = None,
    reasoning_effort: str = "medium",
    gitnexus_binary: str = "gitnexus",
    required_gitnexus_tools: tuple[str, ...] = (),
) -> str:
    """Run one read-only Codex turn and return validated-shape JSON text."""
    if reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError("reasoning_effort must be low, medium, or high")
    with tempfile.TemporaryDirectory(prefix="gitea-review-", dir=temp_root) as directory:
        workdir = Path(directory)
        schema_path = workdir / "review-schema.json"
        output_path = workdir / "review.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            *executable_command(codex_binary),
            "exec",
            "-",
            "--config",
            f'model_reasoning_effort="{reasoning_effort}"',
            *mcp_config(gitnexus_binary, repository),
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
            "--json",
            "--cd",
            str(repository),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                input=prompt,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=safe_codex_environment(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Codex CLI was not found: {codex_binary}") from exc
        if result.returncode != 0:
            detail = " ".join(result.stderr.split())[-2000:]
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Codex analysis failed with exit code {result.returncode}{suffix}")
        completed_tools = _completed_mcp_tools(result.stdout, "gitnexus")
        missing_tools = [tool for tool in required_gitnexus_tools if tool not in completed_tools]
        if missing_tools:
            raise RuntimeError(
                f"Codex review did not complete required GitNexus tools: {', '.join(missing_tools)}"
            )
        try:
            raw = output_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError("Codex did not produce a structured result") from exc
        if fixed_fields:
            value = json.loads(raw)
            value.update(fixed_fields)
            raw = json.dumps(value)
        return raw


def _completed_mcp_tools(events: str, server: str) -> set[str]:
    completed: set[str] = set()
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) and event.get("type") == "item.completed" else None
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        if (item.get("server") or item.get("server_name")) != server:
            continue
        if item.get("status") not in {None, "completed"} or item.get("error"):
            continue
        tool = item.get("tool") or item.get("tool_name")
        if isinstance(tool, str):
            completed.add(tool)
    return completed
