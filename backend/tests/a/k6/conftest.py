from pathlib import Path

import pytest

from wg.k6.clock import start_clock
from wg.k6.db import Database
from wg.k6.migrate import migrate

from .helpers import EPOCH


@pytest.fixture
def blank_db(tmp_path: Path):
    """A migrated database with no rows."""
    db = Database(tmp_path / "wg.db")
    migrate(db)
    yield db
    db.close()


@pytest.fixture
def db(blank_db: Database) -> Database:
    """A migrated database whose simulated clock stands at the fixture epoch."""
    with blank_db.unit_of_work("test clock") as uow:
        start_clock(uow, EPOCH)
    return blank_db
