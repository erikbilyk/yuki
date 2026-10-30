"""Reading a run directory: the manifest, the episode files and the database copies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA = 1


class ScoreInputError(Exception):
    """The run directory is missing or does not hold episode files this scorer understands."""


@dataclass(frozen=True)
class EpisodeFile:
    data: dict[str, Any]
    db_path: Path | None

    @property
    def episode_id(self) -> str:
        return str(self.data["episode_id"])


@dataclass(frozen=True)
class Run:
    path: Path
    manifest: dict[str, Any]
    episodes: tuple[EpisodeFile, ...]

    @property
    def run_id(self) -> str:
        return str(self.manifest.get("run_id") or self.path.name)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScoreInputError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ScoreInputError(f"{path} is not a JSON object")
    return value


def _episode(path: Path) -> EpisodeFile:
    data = _read_json(path)
    if data.get("schema_version") != SUPPORTED_SCHEMA:
        raise ScoreInputError(
            f"{path.name}: unsupported schema_version {data.get('schema_version')!r}"
        )
    name = (data.get("files") or {}).get("database")
    db_path = path.parent / name if name else None
    if db_path is not None and not db_path.is_file():
        db_path = None
    return EpisodeFile(data=data, db_path=db_path)


def load_run(run_dir: Path) -> Run:
    """Every `episodes/*.json` of the run, in file name order."""
    if not run_dir.is_dir():
        raise ScoreInputError(f"{run_dir} is not a directory")
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    paths = sorted((run_dir / "episodes").glob("*.json"))
    if not paths:
        raise ScoreInputError(f"{run_dir} has no episode files")
    return Run(
        path=run_dir,
        manifest=manifest,
        episodes=tuple(_episode(path) for path in paths),
    )
