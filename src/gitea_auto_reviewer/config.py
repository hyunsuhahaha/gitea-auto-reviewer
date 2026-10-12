"""Repository review settings read from the trusted base commit's ``.ai-review.toml``."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .git_context import validate_sha

CONFIG_PATH = ".ai-review.toml"
LANGUAGES = ("en", "ko")
EFFORTS = ("low", "medium", "high")
DEFAULT_DOCUMENTATION_PATHS = ("*.md", "*.rst", "docs/*", "LICENSE")

_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_SETTING_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
# PR code receives pass_env values, so names that usually carry runner credentials are refused.
_CREDENTIAL_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CODEX", "GITEA", "FORGEJO",
                          "GITHUB", "OPENAI", "API_KEY", "PRIVATE_KEY", "CREDENTIAL")


@dataclass(frozen=True)
class ReviewConfig:
    language: str = "en"
    first_pass_effort: str = "medium"
    plan_effort: str = "medium"
    verify_effort: str = "low"
    repair_effort: str = "medium"
    skip_label: str = "skip-ai-review"
    skip_drafts: bool = True
    documentation_paths: tuple[str, ...] = DEFAULT_DOCUMENTATION_PATHS
    django_settings_module: str | None = None
    pass_env: tuple[str, ...] = ()
    required_settings: tuple[str, ...] = ()
    reproduction_timeout: int = 180

    def to_json(self) -> str:
        value = asdict(self)
        for name in ("documentation_paths", "pass_env", "required_settings"):
            value[name] = list(value[name])
        return json.dumps(value, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> ReviewConfig:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("review config is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("review config does not match the resolved schema")
        return _validated(
            language=value["language"],
            first_pass_effort=value["first_pass_effort"],
            plan_effort=value["plan_effort"],
            verify_effort=value["verify_effort"],
            repair_effort=value["repair_effort"],
            skip_label=value["skip_label"],
            skip_drafts=value["skip_drafts"],
            documentation_paths=value["documentation_paths"],
            django_settings_module=value["django_settings_module"],
            pass_env=value["pass_env"],
            required_settings=value["required_settings"],
            reproduction_timeout=value["reproduction_timeout"],
        )


def parse_config(text: str | None) -> ReviewConfig:
    """Validate ``.ai-review.toml`` text; ``None`` (no file) yields the defaults."""
    if text is None:
        return ReviewConfig()
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{CONFIG_PATH} is not valid TOML: {exc}") from exc
    _keys(document, {"language", "reasoning", "skip", "django", "evidence", "reproduction"}, CONFIG_PATH)
    reasoning = _table(document, "reasoning", {"first_pass", "plan", "verify", "repair"})
    skip = _table(document, "skip", {"label", "drafts", "documentation_paths"})
    django = _table(document, "django", {"settings_module"})
    evidence = _table(document, "evidence", {"pass_env"})
    reproduction = _table(document, "reproduction", {"timeout", "required_settings"})
    settings = reproduction.get("required_settings", {})
    if not isinstance(settings, dict):
        raise ValueError("reproduction.required_settings must be a table")
    defaults = ReviewConfig()
    return _validated(
        language=document.get("language", defaults.language),
        first_pass_effort=reasoning.get("first_pass", defaults.first_pass_effort),
        plan_effort=reasoning.get("plan", defaults.plan_effort),
        verify_effort=reasoning.get("verify", defaults.verify_effort),
        repair_effort=reasoning.get("repair", defaults.repair_effort),
        skip_label=skip.get("label", defaults.skip_label),
        skip_drafts=skip.get("drafts", defaults.skip_drafts),
        documentation_paths=skip.get("documentation_paths", list(defaults.documentation_paths)),
        django_settings_module=django.get("settings_module"),
        pass_env=evidence.get("pass_env", []),
        required_settings=[f"{name}={_setting_value(name, value)}" for name, value in settings.items()],
        reproduction_timeout=reproduction.get("timeout", defaults.reproduction_timeout),
    )


def load_base_config(repository: Path, base_sha: str) -> ReviewConfig:
    """Read the config blob from the base commit without touching the PR checkout."""
    base_sha = validate_sha(base_sha)
    result = subprocess.run(["git", "cat-file", "-e", f"{base_sha}:{CONFIG_PATH}"], cwd=repository,
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ReviewConfig()
    blob = subprocess.run(["git", "show", f"{base_sha}:{CONFIG_PATH}"], cwd=repository,
                          capture_output=True, encoding="utf-8", errors="strict", check=False)
    if blob.returncode != 0:
        raise RuntimeError(f"could not read {CONFIG_PATH} from the base commit")
    return parse_config(blob.stdout)


def _validated(**value: object) -> ReviewConfig:
    language = value["language"]
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    for name in ("first_pass_effort", "plan_effort", "verify_effort", "repair_effort"):
        if value[name] not in EFFORTS:
            raise ValueError(f"{name} must be low, medium, or high")
    label = value["skip_label"]
    if not isinstance(label, str) or len(label) > 100 or "\n" in label:
        raise ValueError("skip label must be a short single-line string")
    if not isinstance(value["skip_drafts"], bool):
        raise ValueError("skip drafts must be true or false")
    documentation = _strings(value["documentation_paths"], "documentation_paths", 50, 200)
    module = value["django_settings_module"]
    if module is not None and (not isinstance(module, str) or not _MODULE_NAME.fullmatch(module)):
        raise ValueError("django.settings_module must be a dotted Python module name")
    pass_env = _strings(value["pass_env"], "evidence.pass_env", 50, 64)
    for name in pass_env:
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name in evidence.pass_env: {name}")
        if any(part in name for part in _CREDENTIAL_NAME_PARTS):
            raise ValueError(f"evidence.pass_env must not expose runner credentials: {name}")
    required = _strings(value["required_settings"], "reproduction.required_settings", 50, 300)
    for item in required:
        name, separator, _ = item.partition("=")
        if not separator or not _SETTING_NAME.fullmatch(name):
            raise ValueError(f"invalid required setting: {item}")
    timeout = value["reproduction_timeout"]
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("reproduction.timeout must be an integer between 1 and 3600")
    return ReviewConfig(language, value["first_pass_effort"], value["plan_effort"],  # type: ignore[arg-type]
                        value["verify_effort"], value["repair_effort"], label, value["skip_drafts"],
                        documentation, module, pass_env, required, timeout)


def _keys(value: dict, allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown keys in {name}: {', '.join(unknown)}")


def _table(document: dict, name: str, allowed: set[str]) -> dict:
    table = document.get(name, {})
    if not isinstance(table, dict):
        raise ValueError(f"[{name}] must be a table")
    _keys(table, allowed, f"[{name}]")
    return table


def _strings(value: object, name: str, max_items: int, max_length: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > max_items:
        raise ValueError(f"{name} must be a list of at most {max_items} strings")
    if any(not isinstance(item, str) or not item or len(item) > max_length or "\n" in item for item in value):
        raise ValueError(f"{name} contains an invalid entry")
    return tuple(dict.fromkeys(value))


def _setting_value(name: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return str(value)
    raise ValueError(f"reproduction.required_settings.{name} must be a string, integer, or boolean")
