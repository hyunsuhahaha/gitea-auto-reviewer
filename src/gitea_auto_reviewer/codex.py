"""Minimal, credential-isolated Codex CLI wrapper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .review import REVIEW_JSON_SCHEMA, Review

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


def safe_codex_environment() -> dict[str, str]:
    allowed = {name.upper() for name in SAFE_ENVIRONMENT_NAMES}
    return {name: value for name, value in os.environ.items() if name.upper() in allowed}


def _codex_command(binary: str) -> list[str]:
    if os.name == "nt" and Path(binary).suffix.lower() != ".exe":
        shim_name = binary if Path(binary).suffix.lower() == ".cmd" else f"{binary}.cmd"
        shim = shutil.which(shim_name)
        if shim:
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            return [str(Path(system_root) / "System32" / "cmd.exe"), "/d", "/s", "/c", shim]
    return [shutil.which(binary) or binary]


def assert_logged_in(codex_binary: str = "codex") -> None:
    try:
        result = subprocess.run(
            [*_codex_command(codex_binary), "login", "status"],
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
) -> Review:
    with tempfile.TemporaryDirectory(prefix="gitea-review-", dir=temp_root) as directory:
        workdir = Path(directory)
        schema_path = workdir / "review-schema.json"
        output_path = workdir / "review.json"
        schema_path.write_text(json.dumps(REVIEW_JSON_SCHEMA), encoding="utf-8")
        command = [
            *_codex_command(codex_binary),
            "exec",
            "-",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
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
            raise RuntimeError(f"Codex review failed with exit code {result.returncode}{suffix}")
        try:
            raw = output_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError("Codex did not produce a review result") from exc
        return Review.from_json(raw)
