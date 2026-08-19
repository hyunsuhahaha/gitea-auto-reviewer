from pathlib import Path
from subprocess import CompletedProcess

import pytest

from gitea_auto_reviewer.gitnexus import index_repository, mcp_config


def test_index_is_bound_to_exact_head(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return CompletedProcess(command, 0, f"{'b' * 40}\n", "")
        return CompletedProcess(command, 0, "indexed", "")

    monkeypatch.setattr("gitea_auto_reviewer.gitnexus.subprocess.run", fake_run)
    monkeypatch.setattr("gitea_auto_reviewer.gitnexus.executable_command", lambda _: ["gitnexus"])

    index_repository(tmp_path, "b" * 40)

    assert calls[1] == [
        "gitnexus", "analyze", str(tmp_path.resolve()),
        "--skip-embeddings", "--skip-agents-md", "--skip-skills",
    ]


def test_index_rejects_wrong_head(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gitea_auto_reviewer.gitnexus.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0, f"{'a' * 40}\n", ""),
    )
    with pytest.raises(ValueError, match="PR head SHA"):
        index_repository(tmp_path, "b" * 40)


def test_mcp_config_scopes_gitnexus_to_repository(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("gitea_auto_reviewer.gitnexus.executable_command", lambda _: ["gitnexus"])
    config = " ".join(mcp_config("gitnexus", tmp_path))

    assert "mcp_servers.gitnexus.command" in config
    assert "GITNEXUS_MCP_READ_ONLY" in config
    assert "GITNEXUS_MCP_ALLOWED_REPOS" in config
    assert "mcp_servers.gitnexus.required=true" in config
    assert 'enabled_tools=["detect_changes","context","impact","trace"]' in config
    assert str(tmp_path.resolve()).replace("\\", "\\\\") in config
