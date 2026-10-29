import { useCallback, useEffect, useState } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { useInterval } from "./hooks.ts";
import { Money } from "./Money.tsx";
import { formatFen } from "./money.ts";
import { S } from "./strings.ts";
import type { CashDetails, CashSnapshotView } from "./types.ts";

const REFRESH_MS = 8000;

export function CashPanel({ refreshKey }: { refreshKey: number }) {
  const [snapshot, setSnapshot] = useState<CashSnapshotView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(() => {
    api
      .snapshot()
      .then((value) => {
        setSnapshot(value);
        setError(null);
      })
      .catch((e: unknown) => setError(errorText(e)));
  }, []);

  useEffect(load, [load, refreshKey]);
  useInterval(load, REFRESH_MS);

  if (!snapshot) {
    return (
      <section className="panel" aria-label={S.cash.title}>
        <h2>{S.cash.title}</h2>
        {error ? <p className="error">{error}</p> : <p className="muted">{S.loading}</p>}
      </section>
    );
  }

  const shortfall = snapshot.mode === "shortfall";
  const next = snapshot.next_operation;
  return (
    <section className="panel" aria-label={S.cash.title}>
      <h2>{S.cash.title}</h2>
      {error && <p className="error">{error}</p>}
      <dl className="fields">
        <div className={shortfall ? "field field-alert" : "field field-main"}>
          <dt>{shortfall ? S.cash.shortfall : S.cash.headroom}</dt>
          <dd data-testid={shortfall ? "field-shortfall" : "field-headroom"}>
            <Money fen={shortfall ? snapshot.shortfall_fen : snapshot.headroom_fen} />
          </dd>
        </div>
        <div className="field">
          <dt>{S.cash.protectedFunds}</dt>
          <dd data-testid="field-protected">
            <Money fen={snapshot.protected_fen} />
          </dd>
        </div>
        <div className="field">
          <dt>{S.cash.nextOperation}</dt>
          <dd data-testid="field-next">
            {next ? (
              <>
                {S.kinds[next.kind]}
                {next.amount_fen !== null && (
                  <>
                    {" "}
                    <Money fen={next.amount_fen} />
                  </>
                )}
                <span className="sub">{S.phrases[next.phrase]}</span>
              </>
            ) : (
              S.cash.none
            )}
          </dd>
        </div>
        <div className="field">
          <dt>{S.cash.unpaidBills}</dt>
          <dd data-testid="field-bills">
            <Money fen={snapshot.unpaid_bills_fen} />
            <span className="sub">{S.cash.billsCount(snapshot.unpaid_bills_count)}</span>
          </dd>
        </div>
      </dl>
      <p className="muted small">
        {S.cash.asOf}：{formatTime(snapshot.as_of)}
      </p>
      <button type="button" className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? S.cash.detailsClose : S.cash.detailsOpen}
      </button>
      {open && <Details details={snapshot.details} shortfall={shortfall} />}
    </section>
  );
}

function Details({ details, shortfall }: { details: CashDetails; shortfall: boolean }) {
  const quality = details.quality;
  const names = new Map(details.accounts.map((a) => [a.account_id, a.name]));
  const rows: [string, number][] = [
    [S.cash.c, details.c_fen],
    [S.cash.e, details.e_fen],
    [S.cash.l, details.l_fen],
    [S.cash.b, details.b_fen],
    [S.cash.m, details.m_fen],
    [S.cash.u, details.u_fen],
  ];
  return (
    <div className="details" data-testid="cash-details">
      <h3>{S.cash.formulaTitle}</h3>
      <p className="small">{S.cash.formulaProtected}</p>
      <p className="small">{shortfall ? S.cash.formulaShortfall : S.cash.formulaHeadroom}</p>

      <h3>{S.cash.components}</h3>
      <table>
        <tbody>
          {rows.map(([label, fen]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td className="num">
                <Money fen={fen} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>{S.cash.accounts}</h3>
      <table>
        <thead>
          <tr>
            <th scope="col" />
            <th scope="col">{S.cash.accountPosted}</th>
            <th scope="col">{S.cash.accountAvailable}</th>
          </tr>
        </thead>
        <tbody>
          {details.accounts.map((a) => (
            <tr key={a.account_id}>
              <th scope="row">
                {a.name}
                <span className="sub">
                  {a.covered ? S.cash.covered : S.cash.notCovered}
                  {a.reconciled !== null && `，${a.reconciled ? S.cash.reconciled : S.cash.notReconciled}`}
                </span>
              </th>
              <td className="num">
                <Money fen={a.posted_fen} />
              </td>
              <td className="num">
                <Money fen={a.available_fen} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>{S.cash.monthly}</h3>
      <p>
        <Money fen={details.monthly_estimate_fen} /> <span className="tag tag-estimated">{S.cash.estimate}</span>
      </p>

      <h3>{S.cash.quality}</h3>
      <ul className="small">
        <li className={quality.incomplete ? "error" : undefined}>
          {quality.incomplete ? S.cash.qualityIncomplete : S.cash.qualityComplete}
        </li>
        <li>
          {quality.unresolved_count > 0
            ? S.cash.unresolved(quality.unresolved_count, formatFen(quality.unresolved_impact_fen))
            : S.cash.noUnresolved}
        </li>
        {quality.uncovered_accounts.length > 0 && (
          <li>
            {S.cash.uncovered}：{quality.uncovered_accounts.map((id) => names.get(id) ?? id).join("、")}
          </li>
        )}
        {quality.assumptions.length > 0 && (
          <li>
            {S.cash.assumptions}：{quality.assumptions.length}
          </li>
        )}
      </ul>
    </div>
  );
}
