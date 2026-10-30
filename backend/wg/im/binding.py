"""Channel identity: pairing codes and the binding of a Feishu user to a customer (SPEC §21).

A code is shown in the authenticated web app, lasts five simulated minutes and binds once.
Only its HMAC is stored.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta

from wg.contracts.api import PairingCodeView
from wg.contracts.enums import AuditActor
from wg.core.hashes import hmac_hex
from wg.k6 import audit, channel
from wg.k6.db import Database
from wg.k6.simtime import now

FEISHU = "feishu"
PAIRING_TTL = timedelta(minutes=5)

_BIND_RE = re.compile(r"^\s*绑定\s*(\d{6})\s*$")
_ATTEMPTS = 8


def parse_bind_command(text: str) -> str | None:
    """The six-digit code of a message "绑定 123456", else None."""
    found = _BIND_RE.match(text)
    return None if found is None else found.group(1)


def pairing_hmac(key: str, code: str) -> str:
    return hmac_hex(key, f"pairing:{code}")


def issue_pairing_code(db: Database, key: str, customer_id: str) -> PairingCodeView:
    """A new code for the customer; their earlier unused codes stop working."""

    def issue(uow) -> PairingCodeView:
        at = now(uow)
        channel.close_open_codes(uow, customer_id, at)
        expires_at = at + PAIRING_TTL
        for _ in range(_ATTEMPTS):
            code = f"{secrets.randbelow(10**6):06d}"
            if channel.insert_pairing_code(uow, pairing_hmac(key, code), customer_id, expires_at):
                audit.record(
                    uow, AuditActor.CUSTOMER, "pairing_code_issued", customer_id=customer_id
                )
                return PairingCodeView(code=code, expires_at=expires_at)
        raise RuntimeError("no free pairing code")

    return db.run_in_uow(issue, "issue pairing code")


def bind_user(db: Database, key: str, external_user_id: str, code: str) -> str | None:
    """Bind the Feishu user to the code's customer and return the customer; None if refused.

    A code that is unknown, used or expired is refused. A user bound to another customer is
    moved to this one.
    """

    def bind(uow) -> str | None:
        at = now(uow)
        hashed = pairing_hmac(key, code)
        row = channel.find_pairing_code(uow, hashed)
        if row is None or not channel.consume_pairing_code(uow, hashed, at):
            audit.record(
                uow,
                AuditActor.SYSTEM,
                "im_bind_refused",
                customer_id=row.customer_id if row else None,
                ref=external_user_id[:64],
                decision="used_or_expired" if row else "unknown_code",
            )
            return None
        channel.revoke_binding(uow, FEISHU, external_user_id, at)
        channel.insert_binding(uow, FEISHU, external_user_id, row.customer_id, at)
        audit.record(
            uow,
            AuditActor.SYSTEM,
            "im_bound",
            customer_id=row.customer_id,
            ref=external_user_id[:64],
        )
        return row.customer_id

    return db.run_in_uow(bind, "bind im user")
