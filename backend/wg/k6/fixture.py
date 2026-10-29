"""Load a fixture profile into the database (SPEC §23).

`load_into_empty` fills an empty database in one unit of work; `reset` wipes every table
and loads in one unit, so a failed reset leaves the previous state. Login sessions survive
a reset when their customer or operator exists in the new profile. Opening balances are
posted as one balanced OPENING operation at the epoch.

Command line:
    uv run python -m wg.k6.fixture load --profile fixture_v1 [--variant NAME] [--db PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from wg.contracts.enums import AuditActor, OperationEventKind, OperationStatus
from wg.contracts.fixture import Fixture, FixtureError, FixtureStatement, load_fixture
from wg.contracts.ids import IdPrefix, new_id
from wg.contracts.tools import READ_SCOPES
from wg.k6 import audit, imports, sessions
from wg.k6.accounts import OWN_PAYEE_MASK, own_payee_id
from wg.k6.categories import seed_categories
from wg.k6.clock import start_clock
from wg.k6.db import Database, Reader, UnitOfWork, open_database
from wg.k6.migrate import migrate
from wg.k6.passwords import hash_password
from wg.k6.reserves import RESERVE_KIND
from wg.k6.simtime import ClockState, to_db
from wg.settings import REPO_ROOT, load_settings

FIXTURE_DIR = REPO_ROOT / "backend" / "tests" / "fixtures"

_PROFILE_NAME = re.compile(r"^[a-z0-9_]{1,64}$")

OPENING_EQUITY = "acc_sys_opening_equity"
INTERNAL_ACCOUNTS = {
    OPENING_EQUITY: "opening equity",
    "acc_sys_clearing": "interbank clearing",
    "acc_sys_fund_settlement": "fund settlement",
    "acc_sys_fee_income": "fee income",
}
DEFAULT_PROFILE = "fixture_v1"


class NotEmptyError(RuntimeError):
    """The database already holds a profile; use reset instead."""


def profile_path(profile: str) -> Path:
    if not _PROFILE_NAME.match(profile):
        raise FixtureError(f"invalid profile name {profile!r}")
    return FIXTURE_DIR / f"{profile}.yaml"


def read_profile(profile: str, variant: str | None = None) -> Fixture:
    """Read and validate the named profile, with the variant applied; no database access."""
    path = profile_path(profile)
    if not path.is_file():
        raise FixtureError(f"unknown profile {profile!r}")
    fixture = load_fixture(path, variant)
    if fixture.profile != profile:
        raise FixtureError(f"{path.name} declares profile {fixture.profile!r}")
    return fixture


def is_empty(reader: Reader) -> bool:
    clock = reader.execute("SELECT 1 FROM sim_clock").fetchone()
    customers = reader.execute("SELECT 1 FROM customers").fetchone()
    return clock is None and customers is None


def hash_logins(fixture: Fixture) -> dict[str, str]:
    """Salted hashes by login handle, computed before the unit of work opens."""
    logins = [*fixture.customers, *fixture.operators]
    handles = [login.handle for login in logins]
    if len(handles) != len(set(handles)):
        raise FixtureError("login handles must be unique across customers and operators")
    return {login.handle: hash_password(login.password) for login in logins}


def load_into_empty(db: Database, fixture: Fixture, variant: str | None = None) -> ClockState:
    hashes = hash_logins(fixture)
    with db.unit_of_work("fixture load") as uow:
        if not is_empty(uow):
            raise NotEmptyError(f"{db.path} already holds a profile")
        return load_profile(uow, fixture, variant, hashes)


def reset(db: Database, fixture: Fixture, variant: str | None = None) -> ClockState:
    """Replace the whole database content with the profile, in one unit of work."""
    hashes = hash_logins(fixture)
    with db.unit_of_work("fixture reset") as uow:
        wipe(uow)
        state = load_profile(uow, fixture, variant, hashes)
        sessions.delete_orphaned_sessions(uow)
        return state


def wipe(uow: UnitOfWork) -> None:
    """Delete every row except the schema version and the sessions.

    Foreign keys are checked at commit, after the profile is loaded again.
    """
    uow.execute("PRAGMA defer_foreign_keys = ON")
    tables = uow.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' AND name NOT IN ('schema_version', 'sessions')"
    ).fetchall()
    for (name,) in tables:
        uow.execute(f'DELETE FROM "{name}"')


def load_profile(
    uow: UnitOfWork, fixture: Fixture, variant: str | None, hashes: dict[str, str]
) -> ClockState:
    """Insert every record of the profile into an empty database inside `uow`."""
    clock = start_clock(uow, fixture.epoch)
    loader = _Loader(uow, fixture, hashes, to_db(fixture.epoch), clock.epoch_date)
    loader.run(variant)
    return clock


class _Loader:
    def __init__(
        self,
        uow: UnitOfWork,
        fixture: Fixture,
        hashes: dict[str, str],
        at: str,
        epoch_date: date,
    ) -> None:
        self.uow = uow
        self.fx = fixture
        self.hashes = hashes
        self.at = at
        self.epoch_date = epoch_date
        # Profile-level values (reserve, allowance, margin, plan constants) belong to the
        # first customer, the profile's owner (SPEC §23.2.1).
        self.owner = fixture.customers[0].id

    def day(self, offset: int) -> str:
        return (self.epoch_date + timedelta(days=offset)).isoformat()

    def run(self, variant: str | None) -> None:
        self.uow.execute(
            "INSERT INTO sim_profile (id, profile, variant, created_at) VALUES (1, ?, ?, ?)",
            (self.fx.profile, variant, self.at),
        )
        self.people()
        self.accounts()
        self.payees()
        self.settings()
        seed_categories(self.uow)
        self.statements()
        self.commitments()
        self.funds()
        self.read_scopes()
        audit.record(
            self.uow,
            AuditActor.SYSTEM,
            "fixture_loaded",
            ref=self.fx.profile,
            decision=variant,
        )

    def people(self) -> None:
        for c in self.fx.customers:
            self.uow.execute(
                "INSERT INTO customers (customer_id, handle, password_hash, display_name,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (c.id, c.handle, self.hashes[c.handle], c.display_name, self.at),
            )
        for o in self.fx.operators:
            self.uow.execute(
                "INSERT INTO operators (operator_id, handle, password_hash, role, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (o.id, o.handle, self.hashes[o.handle], o.role.value, self.at),
            )

    def accounts(self) -> None:
        for account_id, name in INTERNAL_ACCOUNTS.items():
            self.uow.execute(
                "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen, covered,"
                " created_at) VALUES (?, NULL, 'internal', ?, 0, 0, ?)",
                (account_id, name, self.at),
            )
        for a in self.fx.accounts:
            self.uow.execute(
                "INSERT INTO accounts (account_id, customer_id, kind, name, balance_fen, covered,"
                " created_at) VALUES (?, ?, 'customer', ?, 0, ?, ?)",
                (a.id, a.customer, a.name, int(a.covered), self.at),
            )
        self.opening()

    def opening(self) -> None:
        """One balanced OPENING operation: each opening balance against opening equity."""
        postings = [(a.id, a.opening_fen) for a in self.fx.accounts if a.opening_fen > 0]
        total = sum(amount for _, amount in postings)
        if total == 0:
            return
        request = json.dumps(postings, separators=(",", ":"), sort_keys=True)
        operation_id = new_id(IdPrefix.OPERATION)
        self.uow.execute(
            "INSERT INTO operations (operation_id, idempotency_key, request_hash, kind,"
            " amount_fen, status, submitted_at, settled_at, created_at)"
            " VALUES (?, ?, ?, 'OPENING', ?, ?, ?, ?, ?)",
            (
                operation_id,
                f"opening:{self.fx.profile}",
                hashlib.sha256(request.encode("utf-8")).hexdigest(),
                total,
                OperationStatus.SETTLED.value,
                self.at,
                self.at,
                self.at,
            ),
        )
        self.uow.execute(
            "INSERT INTO operation_events (operation_id, event, reason, at) VALUES (?, ?, ?, ?)",
            (operation_id, OperationEventKind.SETTLED.value, "opening balances", self.at),
        )
        for account_id, amount in [*postings, (OPENING_EQUITY, -total)]:
            self.uow.execute(
                "INSERT INTO ledger_entries (operation_id, account_id, amount_fen, posted_at)"
                " VALUES (?, ?, ?, ?)",
                (operation_id, account_id, amount, self.at),
            )
            self.uow.execute(
                "UPDATE accounts SET balance_fen = balance_fen + ? WHERE account_id = ?",
                (amount, account_id),
            )

    def payees(self) -> None:
        """The fixture's payees, then one payee per customer account for own transfers."""
        for p in self.fx.payees:
            self.uow.execute(
                "INSERT INTO payees (payee_id, customer_id, alias, display_name, bank,"
                " account_masked, account_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    p.id,
                    p.customer,
                    p.alias,
                    p.display_name,
                    p.bank.value,
                    p.account_masked,
                    p.account_ref,
                    self.at,
                ),
            )
        for a in self.fx.accounts:
            alias = f"本人{a.name}"
            self.uow.execute(
                "INSERT INTO payees (payee_id, customer_id, alias, display_name, bank,"
                " account_masked, account_ref, created_at) VALUES (?, ?, ?, ?, 'same', ?, ?, ?)",
                (own_payee_id(a.id), a.customer, alias, alias, OWN_PAYEE_MASK, a.id, self.at),
            )

    def settings(self) -> None:
        self.uow.execute(
            "INSERT INTO customer_settings (customer_id, margin_fen, plan_constants_json,"
            " created_at) VALUES (?, ?, ?, ?)",
            (
                self.owner,
                self.fx.margin_fen,
                self.fx.plan_constants.model_dump_json(),
                self.at,
            ),
        )
        self.uow.execute(
            "INSERT INTO reserves (customer_id, kind, target_fen, created_at) VALUES (?, ?, ?, ?)",
            (self.owner, RESERVE_KIND, self.fx.reserve.target_fen, self.at),
        )
        allowance = self.fx.allowance
        self.uow.execute(
            "INSERT INTO allowances (customer_id, period, budget_fen, excluded_purpose_ids,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (
                self.owner,
                allowance.period,
                allowance.budget_fen,
                json.dumps(list(allowance.excluded_purpose_ids)),
                self.at,
            ),
        )

    def statements(self) -> None:
        """Statements go through the import service, one batch each."""
        for s in self.fx.statements:
            imports.import_statement(self.uow, statement_of(s, self.epoch_date))

    def commitments(self) -> None:
        for c in self.fx.commitments:
            self.uow.execute(
                "INSERT INTO commitments (commitment_id, customer_id, purpose_id, kind, payee_id,"
                " amount_fen, unpaid_fen, due_date, recurrence, source, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.id,
                    c.customer,
                    c.purpose_id,
                    c.kind,
                    c.payee,
                    c.amount_fen,
                    c.unpaid_fen,
                    self.day(c.due_offset_days),
                    c.recurrence.value,
                    c.source,
                    c.status.value,
                    self.at,
                ),
            )

    def funds(self) -> None:
        for f in self.fx.fee_schedules:
            tiers = [tier.model_dump(mode="json") for tier in f.tiers]
            self.uow.execute(
                "INSERT INTO fee_schedules (fee_schedule_id, terms_version, purchase_rate,"
                " tiers_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (f.id, f.terms_version, _decimal(f.purchase_rate), json.dumps(tiers), self.at),
            )
        for p in self.fx.products:
            self.uow.execute(
                "INSERT INTO products (product_id, name, share_class, nav, nav_date,"
                " fee_schedule_id, min_purchase_fen, redeem_settle_days, purchase_settle_days,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    p.id,
                    p.name,
                    p.share_class,
                    _decimal(p.nav),
                    self.day(p.nav_date_offset_days),
                    p.fee_schedule,
                    p.min_purchase_fen,
                    p.redeem_settle_days,
                    p.purchase_settle_days,
                    self.at,
                ),
            )
        holder: dict[str, list[str]] = {}
        for pos in self.fx.positions:
            holder.setdefault(pos.product, []).append(pos.account)
            self.uow.execute(
                "INSERT INTO positions (account_id, product_id, shares, created_at)"
                " VALUES (?, ?, ?, ?)",
                (pos.account, pos.product, _decimal(pos.shares), self.at),
            )
        for lot in self.fx.lots:
            accounts = holder.get(lot.product, [])
            if len(accounts) != 1:
                raise FixtureError(
                    f"lot {lot.id}: product {lot.product} needs exactly one position"
                )
            self.uow.execute(
                "INSERT INTO lots (lot_id, account_id, product_id, shares, acquired_on, cost_fen,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    lot.id,
                    accounts[0],
                    lot.product,
                    _decimal(lot.shares),
                    self.day(lot.acquired_offset_days),
                    lot.cost_fen,
                    self.at,
                ),
            )
        for snap in self.fx.holdings_snapshots:
            self.uow.executemany(
                "INSERT INTO holdings_snapshots (product_id, as_of, source_id, executable,"
                " taxonomy_version, unresolved_weight, security_code, issuer, sector_code,"
                " weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snap.product,
                        snap.as_of.isoformat(),
                        snap.source_id,
                        int(snap.executable),
                        snap.taxonomy_version,
                        _decimal(snap.unresolved_weight),
                        row.security_code,
                        row.issuer,
                        row.sector,
                        _decimal(row.weight),
                        self.at,
                    )
                    for row in snap.rows
                ],
            )

    def read_scopes(self) -> None:
        self.uow.executemany(
            "INSERT INTO read_scopes (intent, allowed_sources, created_at) VALUES (?, ?, ?)",
            [
                (intent.value, json.dumps(sorted(s.value for s in sources)), self.at)
                for intent, sources in READ_SCOPES.items()
            ],
        )


def statement_of(s: FixtureStatement, epoch_date: date) -> imports.Statement:
    """A fixture statement as the import service takes it; offsets count from the epoch."""
    return imports.Statement(
        statement_id=s.id,
        account_id=s.account,
        period=s.period,
        opening_fen=s.opening_fen,
        closing_fen=s.closing_fen,
        lines=tuple(
            imports.StatementLine(
                source_txn_id=t.source_txn_id,
                posted_on=epoch_date + timedelta(days=t.posted_offset_days),
                amount_fen=t.amount_fen,
                counterparty=t.counterparty,
                description=t.description_raw,
                category_hint=t.category_hint,
            )
            for t in s.transactions
        ),
    )


def _decimal(value: object) -> str:
    """Decimals from the contract models, stored as their fixed-place string."""
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wg.k6.fixture")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("load", help="load a profile into an empty database")
    load.add_argument("--profile", default=DEFAULT_PROFILE)
    load.add_argument("--variant")
    load.add_argument("--db", type=Path, help="database file (default: WG_DB_PATH)")
    args = parser.parse_args(argv)

    if args.db is None:
        args.db = load_settings().db_path
    try:
        fixture = read_profile(args.profile, args.variant)
        db = open_database(args.db)
        version = migrate(db)
        clock = load_into_empty(db, fixture, args.variant)
    except (FixtureError, NotEmptyError) as exc:
        print(f"fixture load failed: {exc}", file=sys.stderr)
        return 1
    variant = args.variant or "-"
    print(
        f"loaded profile={args.profile} variant={variant} schema={version}"
        f" clock={clock.now.isoformat()} db={db.path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
