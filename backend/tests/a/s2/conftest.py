from pathlib import Path

import pytest

from wg.k6 import fixture
from wg.k6.db import Database
from wg.k6.migrate import migrate


@pytest.fixture
def db(tmp_path: Path):
    """A database holding the base profile at the epoch."""
    database = Database(tmp_path / "wg.db")
    migrate(database)
    fixture.load_into_empty(database, fixture.read_profile(fixture.DEFAULT_PROFILE))
    yield database
    database.close()
