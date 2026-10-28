"""Run the backend: `python -m wg.api.serve [--reload]`."""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from wg.api.app import create_app
from wg.settings import SettingsError, load_settings

HOST = "127.0.0.1"
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def build_app() -> FastAPI:
    return create_app(load_settings())


def main(argv: list[str] | None = None) -> int:
    reload = "--reload" in (sys.argv[1:] if argv is None else argv)
    try:
        settings = load_settings()
        settings.require_secrets()
    except SettingsError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(
        "wg.api.serve:build_app",
        factory=True,
        host=HOST,
        port=settings.port,
        reload=reload,
        reload_dirs=[str(PACKAGE_DIR)] if reload else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
