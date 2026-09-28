"""Creating a run reads its own write back consistently, so a lagging replica never 500s it."""

from typing import Any

from app.domains.runs import service as runs_service


class LaggingRepository:
    """A runs repository whose eventually consistent reads have not seen recent writes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.consistent_reads = 0

    def get(self, key: dict[str, Any], *, consistent: bool = False) -> Any:
        """Miss unless the read is strongly consistent, as a lagging replica would."""
        if not consistent:
            return None
        self.consistent_reads += 1
        return self._inner.get(key, consistent=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_new_run_starts_even_when_a_replica_lags(
    auth_client, workspace, uploaded_config_version, state_machine, monkeypatch
):
    """The start reads the row it was just given strongly, so the create returns 201, not 500."""
    original = getattr(runs_service, "_runs")
    lagging: list[LaggingRepository] = []

    def runs(settings: Any) -> LaggingRepository:
        repository = LaggingRepository(original(settings))
        lagging.append(repository)
        return repository

    monkeypatch.setattr(runs_service, "_runs", runs)
    response = auth_client.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "planning"
    assert sum(repository.consistent_reads for repository in lagging) >= 1
