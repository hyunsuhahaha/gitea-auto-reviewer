"""Small Gitea issue-comment API client using the Python standard library."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GiteaAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


Transport = Callable[[urllib.request.Request, float], bytes]


def _default_transport(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class GiteaClient:
    def __init__(
        self,
        base_url: str,
        repository: str,
        token: str,
        transport: Transport = _default_transport,
        timeout: float = 20,
    ) -> None:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("repository must use the owner/name form")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Gitea URL must be an absolute HTTP(S) URL")
        if not token:
            raise ValueError("Gitea token is required")
        root = base_url.rstrip("/")
        self.api_url = root if root.endswith("/api/v1") else f"{root}/api/v1"
        self.repository = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
        self.token = token
        self.transport = transport
        self.timeout = timeout

    def upsert_comment(self, pr_number: int, body: str) -> str:
        if pr_number < 1:
            raise ValueError("PR number must be positive")
        marker = f"<!-- gitea-auto-reviewer:pr={pr_number}:"
        comment_id = self._find_comment(pr_number, marker)
        if comment_id is not None:
            try:
                self._request("PATCH", f"/repos/{self.repository}/issues/comments/{comment_id}", {"body": body})
                return "updated"
            except GiteaAPIError as exc:
                if exc.status != 403:
                    raise
                # A user can imitate the marker, but cannot make the bot edit their comment.
        self._request("POST", f"/repos/{self.repository}/issues/{pr_number}/comments", {"body": body})
        return "created"

    def _find_comment(self, pr_number: int, marker: str) -> int | None:
        # ponytail: inspect at most 500 comments; add full pagination if real PRs exceed this.
        for page in range(1, 11):
            comments = self._request(
                "GET",
                f"/repos/{self.repository}/issues/{pr_number}/comments?limit=50&page={page}",
            )
            if not isinstance(comments, list):
                raise GiteaAPIError(0, "Gitea returned an invalid comment list")
            for comment in comments:
                if isinstance(comment, dict) and marker in str(comment.get("body", "")):
                    identifier = comment.get("id")
                    if isinstance(identifier, int):
                        return identifier
            if len(comments) < 50:
                return None
        return None

    def _request(self, method: str, path: str, body: dict[str, str] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"token {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "gitea-auto-reviewer/0.1",
            },
        )
        try:
            raw = self.transport(request, self.timeout)
        except urllib.error.HTTPError as exc:
            raise GiteaAPIError(exc.code, f"Gitea API request failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GiteaAPIError(0, "Gitea API request failed") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GiteaAPIError(0, "Gitea returned invalid JSON") from exc

