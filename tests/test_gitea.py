import json

from gitea_auto_reviewer.gitea import GiteaAPIError, GiteaClient


def test_upsert_updates_existing_marker() -> None:
    requests = []

    def transport(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return json.dumps(
                [{"id": 7, "body": "<!-- gitea-auto-reviewer:pr=42:sha=old -->"}]
            ).encode()
        return json.dumps({"id": 7}).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    action = client.upsert_comment(42, "<!-- gitea-auto-reviewer:pr=42:sha=new -->\nReview")

    assert action == "updated"
    assert requests[-1].method == "PATCH"
    assert requests[-1].full_url.endswith("/repos/owner/repo/issues/comments/7")


def test_upsert_creates_comment_when_marker_is_absent() -> None:
    methods = []

    def transport(request, timeout):
        methods.append(request.method)
        return b"[]" if request.method == "GET" else b'{"id": 8}'

    client = GiteaClient("https://gitea.example/api/v1", "owner/repo", "token", transport)

    action = client.upsert_comment(42, "<!-- gitea-auto-reviewer:pr=42:sha=new -->\nReview")

    assert action == "created"
    assert methods == ["GET", "POST"]


def test_get_review_comment_returns_existing_body() -> None:
    body = "<!-- gitea-auto-reviewer:pr=42:sha=abc -->\nReview"

    def transport(request, timeout):
        return json.dumps([{"id": 7, "body": body}]).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert client.get_review_comment(42) == body


def test_get_pull_request_returns_review_metadata() -> None:
    def transport(request, timeout):
        assert request.method == "GET"
        assert request.full_url.endswith("/repos/owner/repo/pulls/95")
        return json.dumps({
            "title": "ERP PO allocation",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
        }).encode()

    metadata = GiteaClient("https://gitea.example", "owner/repo", "token", transport).get_pull_request(95)

    assert metadata.number == 95
    assert metadata.title == "ERP PO allocation"
    assert metadata.base_sha == "a" * 40
    assert metadata.head_sha == "b" * 40
    assert metadata.head_repository == "owner/repo"


def test_get_pull_request_uses_merge_commit_parent_when_merged() -> None:
    def transport(request, timeout):
        assert request.method == "GET"
        if request.full_url.endswith("/repos/owner/repo/pulls/95"):
            return json.dumps({
                "title": "ERP PO allocation",
                "base": {"sha": "c" * 40},
                "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
                "merged": True,
                "merge_commit_sha": "d" * 40,
            }).encode()
        assert request.full_url.endswith(f"/repos/owner/repo/git/commits/{'d' * 40}")
        return json.dumps({"parents": [{"sha": "a" * 40}, {"sha": "b" * 40}]}).encode()

    metadata = GiteaClient("https://gitea.example", "owner/repo", "token", transport).get_pull_request(95)

    assert metadata.base_sha == "a" * 40


def test_get_pull_request_rejects_merged_pull_request_without_merge_commit() -> None:
    def transport(request, timeout):
        return json.dumps({
            "title": "ERP PO allocation",
            "base": {"sha": "c" * 40},
            "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
            "merged": True,
        }).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    try:
        client.get_pull_request(95)
        assert False, "expected GiteaAPIError"
    except GiteaAPIError:
        pass
