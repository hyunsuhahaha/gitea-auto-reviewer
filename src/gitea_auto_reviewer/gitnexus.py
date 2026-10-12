"""Index the exact PR head for GitNexus MCP queries."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
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


class GitNexusImpact:
    """Minimal STDIO MCP client that asks GitNexus for call-graph impact without Codex."""

    def __init__(self, binary: str, repository: Path, timeout: int = 120) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self.timeout = timeout
        repo = str(repository.resolve())
        environment = dict(os.environ, GITNEXUS_MCP_READ_ONLY="1", GITNEXUS_MCP_ALLOWED_REPOS=repo,
                           GITNEXUS_MCP_DEFAULT_REPO=repo)
        try:
            self.process = subprocess.Popen(
                [*executable_command(binary), "mcp"], cwd=repository, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"GitNexus CLI was not found: {binary}") from exc
        threading.Thread(target=self._read, daemon=True).start()
        self._next_id = 0
        self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "gitea-auto-reviewer", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for line in self.process.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def _send(self, message: dict) -> None:
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise RuntimeError("GitNexus MCP server stopped") from exc

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                line = self._queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise RuntimeError(f"GitNexus MCP {method} exceeded {self.timeout} seconds") from exc
            if line is None:
                raise RuntimeError("GitNexus MCP server stopped")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"GitNexus MCP {method} failed: {message['error']}")
                return message.get("result", {})

    def __call__(self, file: str, name: str, direction: str) -> set[tuple[str, str]]:
        """Return (filePath, name) of every symbol GitNexus reports in that impact direction."""
        result = self._request("tools/call", {"name": "impact", "arguments": {
            "target": name, "file_path": file, "direction": direction, "includeTests": True,
        }})
        text = "".join(item.get("text", "") for item in result.get("content", []) if isinstance(item, dict))
        try:
            value = json.loads(text.split("\n---", 1)[0])
        except ValueError as exc:
            raise RuntimeError("GitNexus impact returned non-JSON output") from exc
        if value.get("error"):
            return set()
        return {(item.get("filePath", ""), item.get("name", ""))
                for items in (value.get("byDepth") or {}).values() for item in items}

    def close(self) -> None:
        self.process.kill()
        self.process.wait(timeout=10)
