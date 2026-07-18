from __future__ import annotations

from types import SimpleNamespace

import pytest

from oh_my_ruyi import ruyi_facade


class FakeCompositeRepo:
    instances: list["FakeCompositeRepo"] = []

    def __init__(self, entries, config) -> None:
        self.entries = list(entries)
        self.config = config
        self.ensure_calls = 0
        self.synced_ids: list[str] = []
        self.instances.append(self)

    def ensure_git_repo(self) -> None:
        self.ensure_calls += 1

    def sync_one(self, repo_id: str) -> None:
        self.synced_ids.append(repo_id)


@pytest.fixture(autouse=True)
def fake_composite_repo(monkeypatch):
    FakeCompositeRepo.instances.clear()
    monkeypatch.setattr(ruyi_facade, "CompositeRepo", FakeCompositeRepo)


def _entry(repo_id: str, *, active: bool = True):
    return SimpleNamespace(id=repo_id, active=active)


def test_ensure_repo_uses_only_active_ruyisdk_repo() -> None:
    old_repo = object()
    config = SimpleNamespace(
        repo_entries=[
            _entry("third-party"),
            _entry("ruyisdk"),
            _entry("disabled", active=False),
        ],
        repo=old_repo,
    )

    result = ruyi_facade.ensure_repo(config)

    assert [entry.id for entry in result.entries] == ["ruyisdk"]
    assert result.ensure_calls == 1
    assert config.repo is result
    assert config.repo is not old_repo


def test_sync_repo_updates_only_ruyisdk_and_reloads_cache() -> None:
    config = SimpleNamespace(
        repo_entries=[_entry("ruyisdk"), _entry("broken-repo")],
    )
    old_repo = FakeCompositeRepo(config.repo_entries, config)
    config.repo = old_repo

    result = ruyi_facade.sync_repo(config, old_repo)

    assert old_repo.synced_ids == ["ruyisdk"]
    assert result is not old_repo
    assert [entry.id for entry in result.entries] == ["ruyisdk"]
    assert config.repo is result


def test_provision_repo_requires_active_ruyisdk_repo() -> None:
    config = SimpleNamespace(
        repo_entries=[_entry("ruyisdk", active=False), _entry("third-party")],
    )

    with pytest.raises(RuntimeError, match="active metadata repository 'ruyisdk'"):
        ruyi_facade.use_provision_repo(config)
