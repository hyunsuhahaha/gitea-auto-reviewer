"""Build a two-commit git repository from the shop Django fixture.

The base commit refuses orders larger than stock; the head commit drops that check, which is the
data-integrity regression the differential and impact tests look for.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class ShopRepository:
    path: Path
    base_sha: str
    head_sha: str
    database: Path


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
         "-c", "core.autocrlf=false", *arguments],
        cwd=repository, check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_shop_repository(tmp_path: Path, monkeypatch, seed_stock: int = 1) -> ShopRepository:
    repository = tmp_path / "shop"
    shutil.copytree(FIXTURES / "shop", repository)
    _git(repository, "init", "-q")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    shutil.copytree(FIXTURES / "shop_head", repository, dirs_exist_ok=True)
    _git(repository, "commit", "-qam", "head")
    head_sha = _git(repository, "rev-parse", "HEAD")

    database = tmp_path / "shop.sqlite3"
    monkeypatch.setenv("SHOP_DB", str(database))
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config")
    from gitea_auto_reviewer import evidence

    monkeypatch.setattr(evidence, "SAFE_ENVIRONMENT_NAMES", {*evidence.SAFE_ENVIRONMENT_NAMES, "SHOP_DB"})
    subprocess.run([sys.executable, "manage.py", "migrate", "-v", "0"], cwd=repository, check=True)
    subprocess.run(
        [sys.executable, "manage.py", "shell", "-c",
         f"from shop.models import Product; Product.objects.create(name='widget', stock={seed_stock})"],
        cwd=repository, check=True,
    )
    return ShopRepository(repository, base_sha, head_sha, database)
