"""Simulated time: reading the persisted clock and encoding times for the database (SPEC §5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from wg.k6.db import Reader

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ClockError(ValueError):
    """The clock is not set, or a move would take it backwards."""


def to_db(moment: datetime) -> str:
    """Encode an aware datetime in Asia/Shanghai with a fixed width, so text order is time order."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(SHANGHAI).isoformat(timespec="microseconds")


def from_db(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(SHANGHAI)


def wall_now() -> str:
    """Wall-clock time, for records that exist before or outside simulated time."""
    return to_db(datetime.now(UTC))


@dataclass(frozen=True)
class ClockState:
    now: datetime
    epoch: datetime

    @property
    def day(self) -> int:
        """n of T+n: calendar days in Asia/Shanghai from the epoch's date to now."""
        return (self.now.astimezone(SHANGHAI).date() - self.epoch_date).days

    @property
    def epoch_date(self) -> date:
        return self.epoch.astimezone(SHANGHAI).date()


def read_clock(reader: Reader) -> ClockState:
    row = reader.execute("SELECT now, epoch FROM sim_clock WHERE id = 1").fetchone()
    if row is None:
        raise ClockError("the simulated clock is not set; load a fixture first")
    return ClockState(now=from_db(row["now"]), epoch=from_db(row["epoch"]))


def now(reader: Reader) -> datetime:
    """Current simulated time."""
    return read_clock(reader).now


def now_db(reader: Reader) -> str:
    """Current simulated time, encoded for a `created_at` or `at` column."""
    return to_db(now(reader))
