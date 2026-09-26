"""Installing the App: the state, GitHub's confirmation, and the stored installations."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.common.db import repositories
from app.domains.github import service
from tests.domains.github.conftest import SLUG, installation_body


def install(auth_client, installation_id: int = 77) -> dict:
    """Run an install through the setup callback and return the stored installation."""
    state = auth_client.post("/api/v1/github/install-state").json()["state"]
    response = auth_client.post(
        "/api/v1/github/installations",
        json={"installation_id": installation_id, "setup_action": "install", "state": state},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_the_install_state_needs_an_app(auth_client):
    """No credentials, no install URL."""
    response = auth_client.post("/api/v1/github/install-state")
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_APP_NOT_CONFIGURED"


def test_the_install_state_needs_a_slug(auth_client, configure_app):
    """Credentials from the CLI with no slug anywhere cannot build the install URL."""
    configure_app(slug=None)
    response = auth_client.post("/api/v1/github/install-state")
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_APP_SLUG_MISSING"


def test_the_install_url_carries_the_state(auth_client, configure_app):
    """The URL is the App's own install page, with the state GitHub echoes back."""
    configure_app()
    body = auth_client.post("/api/v1/github/install-state").json()
    url = urlparse(body["install_url"])
    assert url.netloc == "github.com"
    assert url.path == f"/apps/{SLUG}/installations/new"
    assert parse_qs(url.query)["state"] == [body["state"]]


def test_the_stored_slug_wins_over_the_fallback(auth_client, configure_app):
    """The row the manifest flow wrote is the slug source."""
    configure_app(slug="fallback-slug")
    repositories.github().put({**service.APP_KEY, "app_id": "1", "slug": "stored-slug"})
    body = auth_client.post("/api/v1/github/install-state").json()
    assert "/apps/stored-slug/" in body["install_url"]


def test_an_install_is_verified_and_stored(auth_client, configure_app, github):
    """The installation GitHub confirms is stored with its account and selection."""
    configure_app()
    github.installations[77] = installation_body(77)
    stored = install(auth_client)
    assert stored["installation_id"] == "77"
    assert stored["account_login"] == "WebbPulse"
    assert stored["account_type"] == "Organization"
    assert stored["repository_selection"] == "selected"
    assert stored["suspended"] is False
    verified = [request for request in github.requests if request.url.path == "/app/installations/77"]
    assert verified and verified[0].headers["authorization"].startswith("Bearer ey")
    listed = auth_client.get("/api/v1/github/installations").json()["items"]
    assert [item["installation_id"] for item in listed] == ["77"]


def test_an_install_without_a_state_is_refused(auth_client, configure_app, github):
    """The redirect is unauthenticated, so the state is the CSRF check."""
    configure_app()
    github.installations[77] = installation_body(77)
    response = auth_client.post("/api/v1/github/installations", json={"installation_id": 77, "setup_action": "install"})
    assert response.status_code == 400
    assert response.json()["error_code"] == "GITHUB_INVALID_STATE"
    assert github.requests == []


def test_an_install_state_works_once(auth_client, configure_app, github):
    """A replayed setup callback is refused."""
    configure_app()
    github.installations[77] = installation_body(77)
    state = auth_client.post("/api/v1/github/install-state").json()["state"]
    payload = {"installation_id": 77, "setup_action": "install", "state": state}
    assert auth_client.post("/api/v1/github/installations", json=payload).status_code == 201
    assert auth_client.post("/api/v1/github/installations", json=payload).status_code == 400


def test_a_manifest_state_cannot_install(auth_client, configure_app, github, monkeypatch):
    """A state issued for creation is not an install state."""
    monkeypatch.setattr(service, "_settings_or_none", lambda settings: None)
    state = auth_client.post("/api/v1/github/app/manifest", json={}).json()["state"]
    monkeypatch.undo()
    configure_app()
    github.installations[77] = installation_body(77)
    response = auth_client.post(
        "/api/v1/github/installations",
        json={"installation_id": 77, "setup_action": "install", "state": state},
    )
    assert response.status_code == 400


def test_an_installation_of_another_app_is_refused(auth_client, configure_app, github):
    """GitHub answering 404 for the App JWT means the id is not ours, and nothing is stored."""
    configure_app()
    state = auth_client.post("/api/v1/github/install-state").json()["state"]
    response = auth_client.post(
        "/api/v1/github/installations",
        json={"installation_id": 99, "setup_action": "install", "state": state},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "GITHUB_INSTALLATION_NOT_THIS_APP"
    assert auth_client.get("/api/v1/github/installations").json()["items"] == []


def test_an_update_refreshes_a_stored_installation_without_a_state(auth_client, configure_app, github):
    """GitHub's `update` redirect carries no state of ours, so it may only refresh."""
    configure_app()
    github.installations[77] = installation_body(77)
    install(auth_client)
    github.installations[77] = installation_body(77, suspended=True)
    response = auth_client.post("/api/v1/github/installations", json={"installation_id": 77, "setup_action": "update"})
    assert response.status_code == 201
    assert response.json()["suspended"] is True


def test_an_update_cannot_add_an_installation(auth_client, configure_app, github):
    """An `update` for an id not stored here is refused."""
    configure_app()
    github.installations[78] = installation_body(78)
    response = auth_client.post("/api/v1/github/installations", json={"installation_id": 78, "setup_action": "update"})
    assert response.status_code == 400
    assert github.requests == []


def test_refresh_drops_an_installation_github_no_longer_has(auth_client, configure_app, github):
    """Uninstalled on GitHub means gone here too."""
    configure_app()
    github.installations[77] = installation_body(77)
    install(auth_client)
    del github.installations[77]
    assert auth_client.post("/api/v1/github/installations/77/refresh").status_code == 404
    assert auth_client.get("/api/v1/github/installations").json()["items"] == []


def test_refresh_rereads_the_installation(auth_client, configure_app, github):
    """A refresh stores what GitHub says now."""
    configure_app()
    github.installations[77] = installation_body(77)
    install(auth_client)
    github.installations[77] = installation_body(77, login="Renamed")
    body = auth_client.post("/api/v1/github/installations/77/refresh").json()
    assert body["account_login"] == "Renamed"


def test_remove_forgets_the_installation(auth_client, configure_app, github):
    """Removing only forgets the row; GitHub is not called."""
    configure_app()
    github.installations[77] = installation_body(77)
    install(auth_client)
    calls = len(github.requests)
    assert auth_client.delete("/api/v1/github/installations/77").status_code == 204
    assert len(github.requests) == calls
    assert auth_client.delete("/api/v1/github/installations/77").status_code == 404


def test_github_failing_is_a_502(auth_client, configure_app, github):
    """A GitHub outage surfaces as a gateway error, never as GitHub's own message."""
    configure_app()
    github.installations[77] = installation_body(77)
    install(auth_client)
    github.failure = 500
    response = auth_client.post("/api/v1/github/installations/77/refresh")
    assert response.status_code == 502
    assert response.json()["error_code"] == "GITHUB_UNAVAILABLE"


def test_the_repositories_are_paged_to_the_end(auth_client, configure_app, github):
    """Every page is read with an installation token and mapped to the API's shape."""
    configure_app()
    github.installations[77] = installation_body(77)
    github.repositories[77] = [
        {
            "id": index,
            "name": f"repo-{index}",
            "full_name": f"WebbPulse/repo-{index}",
            "private": index % 2 == 0,
            "html_url": f"https://github.com/WebbPulse/repo-{index}",
            "default_branch": "main",
            "owner": {"login": "WebbPulse"},
        }
        for index in range(1, 151)
    ]
    install(auth_client)
    response = auth_client.get("/api/v1/github/installations/77/repositories")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 150
    assert items[0] == {
        "id": 1,
        "name": "repo-1",
        "full_name": "WebbPulse/repo-1",
        "private": False,
        "html_url": "https://github.com/WebbPulse/repo-1",
        "default_branch": "main",
    }
    pages = [r.url.params["page"] for r in github.requests if r.url.path == "/installation/repositories"]
    assert pages == ["1", "2"]
    token_calls = [r for r in github.requests if r.url.path.endswith("/access_tokens")]
    assert len(token_calls) == 1


def test_repositories_need_a_stored_installation(auth_client, configure_app, github):
    """An id nobody installed through this page is a 404, not a GitHub call."""
    configure_app()
    assert auth_client.get("/api/v1/github/installations/77/repositories").status_code == 404
    assert github.requests == []
