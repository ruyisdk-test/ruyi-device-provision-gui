"""Read repository configuration and mutate it through ruyi's config API."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ruyi.config import DEFAULT_REPO_BRANCH, DEFAULT_REPO_URL
from ruyi.config.editor import ConfigEditor
from ruyi.config.schema import (
    KEY_REPOS_ACTIVE,
    KEY_REPOS_BRANCH,
    KEY_REPOS_ID,
    KEY_REPOS_LOCAL,
    KEY_REPOS_NAME,
    KEY_REPOS_PRIORITY,
    KEY_REPOS_REMOTE,
)
from ruyi.ruyipkg.repo import DEFAULT_REPO_ID, REPO_ID_PATTERN
from ruyi.utils.xdg_basedir import XDGBaseDir

from .repo_presets import (
    DEFAULT_REPO_FALLBACK_NAME,
    DEFAULT_REPO_OFFICIAL_NAME,
    OFFICIAL_REPO_REMOTES,
    PRESET_REPOS,
    RUYISDK_SOURCE_PRESETS,
    RepoPreset,
    RepoSource,
)

DEFAULT_REPO_REMOTE = DEFAULT_REPO_URL
DEFAULT_REPO_NAME = DEFAULT_REPO_OFFICIAL_NAME


class RepoManagerError(RuntimeError):
    """Raised for invalid repository configuration or unsupported operations."""


@dataclass(frozen=True, slots=True)
class ConfiguredRepo:
    id: str
    name: str
    remote: str | None
    local: str | None
    branch: str | None
    priority: int
    active: bool
    is_default: bool = False
    configured_source: RepoSource | None = None


DEFAULT_REPO_SOURCES = RUYISDK_SOURCE_PRESETS


def user_config_path() -> Path:
    return XDGBaseDir("ruyi").app_config / "config.toml"


def read_configured_repos(path: Path | None = None) -> tuple[ConfiguredRepo, ...]:
    """Read user-local entries in TOML order without modifying the document."""
    path = user_config_path() if path is None else Path(path)
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except FileNotFoundError:
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RepoManagerError(f"failed to read {path}: {exc}") from exc

    repo_data = data.get("repo", {})
    if not isinstance(repo_data, dict):
        raise RepoManagerError("[repo] must be a TOML table")
    configured_source = RepoSource(
        _optional_string(repo_data, "remote"),
        _optional_string(repo_data, "local"),
        _optional_string(repo_data, "branch"),
    )
    default = ConfiguredRepo(
        DEFAULT_REPO_ID,
        default_repo_name(configured_source.remote),
        configured_source.remote or DEFAULT_REPO_REMOTE,
        configured_source.local,
        configured_source.branch or DEFAULT_REPO_BRANCH,
        0,
        not _optional_bool(repo_data, "disabled", False),
        True,
        configured_source,
    )

    raw_repos = data.get("repos", [])
    if not isinstance(raw_repos, list):
        raise RepoManagerError("[[repos]] must be a TOML array of tables")
    configured: list[ConfiguredRepo] = [default]
    seen = {DEFAULT_REPO_ID}
    for index, item in enumerate(raw_repos):
        if not isinstance(item, dict):
            raise RepoManagerError(f"repos entry {index + 1} must be a TOML table")
        repo_id = _required_string(item, "id", index)
        if repo_id in seen:
            continue
        seen.add(repo_id)
        remote = _optional_string(item, "remote")
        local = _optional_string(item, "local")
        branch = _optional_string(item, "branch")
        if remote is None and local is None:
            raise RepoManagerError(f"repo '{repo_id}' must configure remote or local")
        priority = item.get("priority", 0)
        active = item.get("active", True)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise RepoManagerError(f"repo '{repo_id}' priority must be an integer")
        if not isinstance(active, bool):
            raise RepoManagerError(f"repo '{repo_id}' active must be a boolean")
        configured.append(
            ConfiguredRepo(
                repo_id,
                _preset_name(repo_id) or _optional_string(item, "name") or repo_id,
                remote,
                local,
                branch or DEFAULT_REPO_BRANCH,
                priority,
                active,
                configured_source=RepoSource(remote, local, branch),
            )
        )
    return tuple(configured)


def add_repo(
    path: Path,
    preset: RepoPreset,
    source: RepoSource,
    priority: int,
) -> None:
    """Add one preset as an inactive user-local repository."""
    validate_repo_id(preset.id)
    _validate_source(source)
    if any(repo.id == preset.id for repo in read_configured_repos(path)):
        raise RepoManagerError(f"a repo with id '{preset.id}' already exists")

    entry: dict[str, object] = {
        KEY_REPOS_ID: preset.id,
        KEY_REPOS_NAME: preset.name,
        KEY_REPOS_PRIORITY: priority,
        KEY_REPOS_ACTIVE: False,
    }
    if source.remote is not None:
        entry[KEY_REPOS_REMOTE] = source.remote
    if source.branch:
        entry[KEY_REPOS_BRANCH] = source.branch
    if source.local is not None:
        entry[KEY_REPOS_LOCAL] = source.local

    try:
        with ConfigEditor(Path(path)) as editor:
            editor.add_repos_entry(entry)
            editor.stage()
    except Exception as exc:
        raise RepoManagerError(f"failed to add repo '{preset.id}': {exc}") from exc


def edit_default_repo(
    path: Path,
    current: ConfiguredRepo,
    source: RepoSource,
) -> bool:
    """Apply default-source overrides using ruyi's config editor."""
    if not current.is_default:
        raise RepoManagerError("only the default repository supports source editing")
    configured = current.configured_source or RepoSource()
    if source.local != configured.local:
        raise RepoManagerError("local repository path cannot be changed")
    changes = (
        ("repo.remote", configured.remote, source.remote),
        ("repo.branch", configured.branch, source.branch),
    )
    effective_defaults = {
        "repo.remote": DEFAULT_REPO_REMOTE,
        "repo.branch": DEFAULT_REPO_BRANCH,
    }
    resolved_changes = tuple(
        (key, old, None if old is None and new == effective_defaults[key] else new)
        for key, old, new in changes
    )
    if not any(old != new for _key, old, new in resolved_changes):
        return False

    try:
        with ConfigEditor(Path(path)) as editor:
            for key, old, new in resolved_changes:
                if old == new:
                    continue
                if new is None:
                    editor.unset_value(key)
                else:
                    editor.set_value(key, new)
            editor.stage()
    except Exception as exc:
        raise RepoManagerError(f"failed to edit repo '{current.id}': {exc}") from exc
    return True


def edit_repo(
    path: Path,
    repo: ConfiguredRepo,
    source: RepoSource,
    priority: int,
) -> None:
    """Update supported fields of an additional ``[[repos]]`` entry."""
    if repo.is_default:
        raise RepoManagerError("the default repository has a fixed ID and name")
    _validate_source(source)
    if source.local != repo.local:
        raise RepoManagerError("local repository path cannot be changed")
    updates: dict[str, object] = {
        KEY_REPOS_REMOTE: source.remote or "",
        KEY_REPOS_BRANCH: source.branch or DEFAULT_REPO_BRANCH,
        KEY_REPOS_PRIORITY: priority,
    }
    try:
        with ConfigEditor(Path(path)) as editor:
            if not editor.update_repos_entry(repo.id, updates):
                raise RepoManagerError(f"repo '{repo.id}' is not in the user config")
            editor.stage()
    except RepoManagerError:
        raise
    except Exception as exc:
        raise RepoManagerError(f"failed to edit repo '{repo.id}': {exc}") from exc


def remove_repo(path: Path, repo: ConfiguredRepo) -> None:
    """Remove an additional repository without purging cached data."""
    if repo.is_default:
        raise RepoManagerError("the default repository cannot be removed")
    try:
        with ConfigEditor(Path(path)) as editor:
            if not editor.remove_repos_entry(repo.id):
                raise RepoManagerError(f"repo '{repo.id}' is not in the user config")
            editor.stage()
    except RepoManagerError:
        raise
    except Exception as exc:
        raise RepoManagerError(f"failed to remove repo '{repo.id}': {exc}") from exc


def set_enabled(path: Path, repo: ConfiguredRepo, enabled: bool) -> None:
    """Enable or disable a repository through ruyi's config implementation."""
    try:
        with ConfigEditor(Path(path)) as editor:
            if repo.is_default:
                if enabled:
                    editor.unset_value("repo.disabled")
                else:
                    editor.set_value("repo.disabled", True)
            elif not editor.update_repos_entry(repo.id, {KEY_REPOS_ACTIVE: enabled}):
                raise RepoManagerError(f"repo '{repo.id}' is not in the user config")
            editor.stage()
    except RepoManagerError:
        raise
    except Exception as exc:
        raise RepoManagerError(f"failed to edit repo '{repo.id}': {exc}") from exc


def validate_repo_id(repo_id: str) -> None:
    if repo_id == DEFAULT_REPO_ID:
        raise RepoManagerError(f"'{DEFAULT_REPO_ID}' is reserved")
    if REPO_ID_PATTERN.fullmatch(repo_id) is None:
        raise RepoManagerError(f"invalid repo id '{repo_id}'")


def source_label(source: RepoSource | ConfiguredRepo) -> str:
    if source.remote and source.local:
        return f"{source.remote} | {source.local}"
    return source.remote or source.local or ""


def default_repo_name(remote: str | None) -> str:
    """Return the fixed display name for the configured default source."""
    normalized = remote.strip().rstrip("/") if remote else ""
    official_remotes = {url.rstrip("/") for url in OFFICIAL_REPO_REMOTES}
    if normalized in official_remotes:
        return DEFAULT_REPO_OFFICIAL_NAME
    return DEFAULT_REPO_FALLBACK_NAME


def source_matches_preset(configured: RepoSource, preset: RepoSource) -> bool:
    """Match source keys while treating an omitted branch as ruyi's ``main``."""
    if configured.remote is None or preset.remote is None:
        return configured.remote == preset.remote
    return configured.remote.strip().rstrip("/") == preset.remote.strip().rstrip(
        "/"
    ) and (configured.branch or DEFAULT_REPO_BRANCH) == (
        preset.branch or DEFAULT_REPO_BRANCH
    )


def _preset_name(repo_id: str) -> str | None:
    return next((preset.name for preset in PRESET_REPOS if preset.id == repo_id), None)


def _validate_source(source: RepoSource) -> None:
    if source.remote is None and source.local is None:
        raise RepoManagerError("a remote URL or absolute local path is required")
    if source.local is not None and not Path(source.local).is_absolute():
        raise RepoManagerError("local repository path must be absolute")


def _required_string(data: dict, key: str, index: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise RepoManagerError(f"repos entry {index + 1} requires a string '{key}'")
    return value


def _optional_string(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RepoManagerError(f"'{key}' must be a string")
    return value or None


def _optional_bool(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise RepoManagerError(f"'{key}' must be a boolean")
    return value
