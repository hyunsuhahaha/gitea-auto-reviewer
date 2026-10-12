import json
import subprocess

import pytest

from gitea_auto_reviewer.cli import main
from gitea_auto_reviewer.config import ReviewConfig, load_base_config, parse_config

FULL_CONFIG = """
language = "ko"

[reasoning]
first_pass = "high"
plan = "low"
verify = "medium"
repair = "low"

[skip]
label = "no-ai"
drafts = false
documentation_paths = ["*.md", "handbook/*"]

[django]
settings_module = "config.settings.ci"

[evidence]
pass_env = ["DATABASE_URL", "PROJECT_CI"]

[reproduction]
timeout = 90

[reproduction.required_settings]
ERP_LIVE_SEND = false
RETRY_LIMIT = 3
MODE = "dry-run"
"""


def test_missing_config_uses_defaults() -> None:
    config = parse_config(None)
    assert config == ReviewConfig()
    assert config.language == "en"
    assert config.required_settings == ()


def test_full_config_is_parsed() -> None:
    config = parse_config(FULL_CONFIG)

    assert config.language == "ko"
    assert (config.first_pass_effort, config.plan_effort, config.verify_effort, config.repair_effort) == (
        "high", "low", "medium", "low",
    )
    assert config.skip_label == "no-ai" and config.skip_drafts is False
    assert config.documentation_paths == ("*.md", "handbook/*")
    assert config.django_settings_module == "config.settings.ci"
    assert config.pass_env == ("DATABASE_URL", "PROJECT_CI")
    assert config.reproduction_timeout == 90
    assert config.required_settings == ("ERP_LIVE_SEND=false", "RETRY_LIMIT=3", "MODE=dry-run")


def test_resolved_config_round_trips_through_json() -> None:
    config = parse_config(FULL_CONFIG)
    assert ReviewConfig.from_json(config.to_json()) == config


@pytest.mark.parametrize("text", [
    'language = "fr"',
    'unknown = 1',
    '[reasoning]\nfirst_pass = "extreme"',
    '[skip]\nlabels = "x"',
    '[evidence]\npass_env = ["GITEA_REVIEW_TOKEN"]',
    '[evidence]\npass_env = ["CODEX_HOME"]',
    '[evidence]\npass_env = ["AWS_SECRET_ACCESS_KEY"]',
    '[evidence]\npass_env = ["lower-case"]',
    '[django]\nsettings_module = "config/settings.py"',
    '[reproduction]\ntimeout = 0',
    '[reproduction.required_settings]\nNAME = 1.5',
    'not toml = = 1',
])
def test_invalid_config_is_rejected(text) -> None:
    with pytest.raises(ValueError):
        parse_config(text)


def test_resolved_json_rejects_unknown_fields() -> None:
    value = json.loads(ReviewConfig().to_json())
    value["extra"] = True
    with pytest.raises(ValueError):
        ReviewConfig.from_json(json.dumps(value))


def _git(repository, *arguments):
    return subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repository(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / ".ai-review.toml").write_text('language = "ko"\n', encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    # The PR head tries to loosen its own review; only the base commit counts.
    (tmp_path / ".ai-review.toml").write_text('[evidence]\npass_env = ["DATABASE_URL"]\n', encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "head")
    return base


def test_base_config_ignores_pull_request_head(tmp_path) -> None:
    base = _repository(tmp_path)

    config = load_base_config(tmp_path, base)

    assert config.language == "ko" and config.pass_env == ()


def test_base_without_config_uses_defaults(tmp_path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")

    assert load_base_config(tmp_path, _git(tmp_path, "rev-parse", "HEAD")) == ReviewConfig()


def test_config_command_writes_resolved_json(tmp_path) -> None:
    base = _repository(tmp_path)
    output = tmp_path / "resolved.json"

    assert main(["config", "--base-sha", base, "--repo-dir", str(tmp_path), "--output", str(output)]) == 0
    assert ReviewConfig.from_json(output.read_text(encoding="utf-8")).language == "ko"


def test_reasoning_reads_config_below_environment(monkeypatch, tmp_path, capsys) -> None:
    for name in ("FIRST_PASS", "PLAN", "VERIFY", "REPAIR"):
        monkeypatch.delenv(f"AI_REVIEW_{name}_EFFORT", raising=False)
    monkeypatch.setenv("AI_REVIEW_PLAN_EFFORT", "medium")
    path = tmp_path / "config.json"
    path.write_text(parse_config(FULL_CONFIG).to_json(), encoding="utf-8")

    assert main(["reasoning", "--config-file", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "first-pass=high", "plan=medium", "verify=medium", "repair=low",
    ]


def test_example_config_is_valid() -> None:
    from pathlib import Path

    config = parse_config(Path("examples/ai-review.toml").read_text(encoding="utf-8"))
    assert config == ReviewConfig()
