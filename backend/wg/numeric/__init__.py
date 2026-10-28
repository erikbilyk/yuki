"""Pure cash and funds engines. No I/O, no clock, no database."""

from wg.numeric.cash import compute_snapshot

__all__ = ["compute_snapshot"]
