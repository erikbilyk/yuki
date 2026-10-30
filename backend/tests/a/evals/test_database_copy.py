"""The database copy must hold every committed row, also after the writer was killed."""

import sqlite3
import subprocess
import sys
from pathlib import Path

from evals.runner.dbview import copy_database

WRITER = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE t (x INTEGER)")
for i in range(5):
    conn.execute("INSERT INTO t VALUES (?)", (i,))
print("ready", flush=True)
time.sleep(60)
"""


def killed_writer(db: Path) -> None:
    """Leave `db` in WAL mode with its rows only in the `-wal` file, as after a SIGKILL."""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", WRITER, str(db)], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    process.kill()
    process.wait()


def test_rows_that_only_the_wal_holds_survive_in_a_single_file_copy(tmp_path):
    source = tmp_path / "wg.db"
    killed_writer(source)
    assert (tmp_path / "wg.db-wal").stat().st_size > 0

    copy = tmp_path / "episode.db"
    copy_database(source, copy)

    # immutable=1 ignores any sidecar file: the rows must be in the main file itself.
    with sqlite3.connect(f"file:{copy}?mode=ro&immutable=1", uri=True) as conn:
        assert conn.execute("SELECT x FROM t ORDER BY x").fetchall() == [(i,) for i in range(5)]
    # Header bytes 18 and 19 are 2 in WAL mode and 1 otherwise.
    assert copy.read_bytes()[18:20] == b"\x01\x01"
    assert not (tmp_path / "episode.db-wal").exists()


def test_copy_of_a_database_without_a_log(tmp_path):
    source = tmp_path / "plain.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")
    copy_database(source, tmp_path / "copy.db")
    with sqlite3.connect(f"file:{tmp_path / 'copy.db'}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(7,)]
