"""Index the exact PR head for GitNexus MCP queries."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .git_context import validate_sha


def executable_command(binary: str) -> list[str]:
    if os.name == "nt" and Path(binary).suffix.lower() != ".exe":
        shim_name = binary if Path(binary).suffix.lower() == ".cmd" else f"{binary}.cmd"
        shim = shutil.which(shim_name)
        if shim:
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            return [str(Path(system_root) / "System32" / "cmd.exe"), "/d", "/s", "/c", shim]
    return [shutil.which(binary) or binary]


def index_repository(repository: Path, head_sha: str, binary: str = "gitnexus", timeout: int = 900) -> None:
    repository = repository.resolve()
    expected = validate_sha(head_sha)
    if timeout < 1:
        raise ValueError("timeout must be positive")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=False, capture_output=True, text=True
    )
    if actual.returncode or actual.stdout.strip().lower() != expected:
        raise ValueError("repository must be checked out at the supplied PR head SHA")
    try:
        result = subprocess.run(
            [
                *executable_command(binary),
                "analyze",
                str(repository),
                "--skip-embeddings",
                "--skip-agents-md",
                "--skip-skills",
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"GitNexus CLI was not found: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"GitNexus analysis exceeded {timeout} seconds") from exc
    if result.returncode:
        detail = " ".join((result.stderr or result.stdout).split())[-2000:]
        raise RuntimeError(f"GitNexus analysis failed with exit code {result.returncode}: {detail}")


def mcp_config(binary: str, repository: Path) -> list[str]:
    """Return Codex CLI overrides for a repository-scoped GitNexus STDIO server."""
    command = executable_command(binary)
    server_command, server_args = command[0], [*command[1:], "mcp"]
    repo = str(repository.resolve())
    import json

    env = {
        "GITNEXUS_MCP_READ_ONLY": "1",
        "GITNEXUS_MCP_ALLOWED_REPOS": repo,
        "GITNEXUS_MCP_DEFAULT_REPO": repo,
        "GITNEXUS_MCP_DEFAULT_MAX_TOKENS": "12000",
    }
    return [
        "--config", f"mcp_servers.gitnexus.command={json.dumps(server_command)}",
        "--config", f"mcp_servers.gitnexus.args={json.dumps(server_args)}",
        "--config", f"mcp_servers.gitnexus.env={{{','.join(f'{key}={json.dumps(value)}' for key, value in env.items())}}}",
        "--config", "mcp_servers.gitnexus.required=true",
        "--config", "mcp_servers.gitnexus.startup_timeout_sec=30",
        "--config", 'mcp_servers.gitnexus.enabled_tools=["detect_changes","context","impact","trace"]',
    ]
