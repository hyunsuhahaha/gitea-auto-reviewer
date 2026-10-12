import json
import urllib.error

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
                "head": {"sha": "e" * 40, "repo": {"full_name": "owner/repo"}},
                "merged": True,
                "merge_commit_sha": "d" * 40,
            }).encode()
        assert request.full_url.endswith(f"/repos/owner/repo/git/commits/{'d' * 40}")
        return json.dumps({"parents": [{"sha": "a" * 40}, {"sha": "b" * 40}]}).encode()

    metadata = GiteaClient("https://gitea.example", "owner/repo", "token", transport).get_pull_request(95)

    assert metadata.base_sha == "a" * 40
    assert metadata.head_sha == "b" * 40


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


def test_review_comment_lookup_ignores_marker_copied_by_other_users() -> None:
    forged = "<!-- gitea-auto-reviewer:pr=42:sha=abc -->\nforged"
    genuine = "<!-- gitea-auto-reviewer:pr=42:sha=abc -->\ngenuine"

    def transport(request, timeout):
        return json.dumps([
            {"id": 5, "body": forged, "user": {"login": "someone"}},
            {"id": 7, "body": genuine, "user": {"login": "ai-review-bot"}},
        ]).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert client.get_review_comment(42, "ai-review-bot") == genuine
    assert client.get_review_comment(42) == forged


def test_upsert_with_author_patches_only_own_comment() -> None:
    requests = []

    def transport(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return json.dumps([
                {"id": 5, "body": "<!-- gitea-auto-reviewer:pr=42:sha=x -->", "user": {"login": "someone"}},
                {"id": 7, "body": "<!-- gitea-auto-reviewer:pr=42:sha=y -->", "user": {"login": "bot"}},
            ]).encode()
        return b"{}"

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert client.upsert_comment(42, "<!-- gitea-auto-reviewer:pr=42:sha=z -->", "bot") == "updated"
    assert requests[-1].full_url.endswith("/issues/comments/7")


def test_authenticated_login_is_none_without_user_scope() -> None:
    def transport(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert client.get_authenticated_login() is None


def test_authenticated_login_returns_token_owner() -> None:
    def transport(request, timeout):
        assert request.full_url.endswith("/api/v1/user")
        return b'{"login": "ai-review-bot"}'

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert client.get_authenticated_login() == "ai-review-bot"


def test_get_pull_request_reads_draft_and_labels() -> None:
    def transport(request, timeout):
        return json.dumps({
            "title": "t", "draft": True, "labels": [{"name": "skip-ai-review"}, {"bad": 1}],
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
        }).encode()

    metadata = GiteaClient("https://gitea.example", "owner/repo", "token", transport).get_pull_request(1)

    assert metadata.draft is True
    assert metadata.labels == ("skip-ai-review",)


def test_list_changed_files_paginates_and_gives_up_when_too_large() -> None:
    pages = []

    def transport(request, timeout):
        page = int(request.full_url.rsplit("page=", 1)[1])
        pages.append(page)
        count = 50 if page == 1 else 1
        return json.dumps([{"filename": f"f{page}-{index}.py"} for index in range(count)]).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert len(client.list_changed_files(3)) == 51
    assert pages == [1, 2]
    assert client.list_changed_files(3, max_pages=1) is None


def test_list_pull_request_numbers_stops_at_limit() -> None:
    def transport(request, timeout):
        assert "state=all" in request.full_url
        page = int(request.full_url.rsplit("page=", 1)[1])
        return json.dumps([{"number": page * 100 + index} for index in range(50)]).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    numbers = client.list_pull_request_numbers(60)

    assert len(numbers) == 60 and numbers[0] == 100 and numbers[-1] == 209


def test_get_comment_reactions_returns_contents() -> None:
    def transport(request, timeout):
        assert request.full_url.endswith("/issues/comments/7/reactions")
        return json.dumps([{"content": "+1"}, {"content": "-1"}, {"user": {}}]).encode()

    client = GiteaClient("https://gitea.example", "owner/repo", "token", transport)

    assert client.get_comment_reactions(7) == ("+1", "-1")
