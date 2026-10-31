import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api.ts";
import { openCard } from "./drafts.ts";
import { errorText } from "./errors.ts";
import { formatDate, formatTime } from "./format.ts";
import { Money } from "./Money.tsx";
import { parseYuanToFen } from "./money.ts";
import { S } from "./strings.ts";
import type { DebitDecision, DebitRequestView, DraftResult, MandateChange, MandateView } from "./types.ts";

interface Props {
  refreshKey: number;
  onChange: () => void;
  onCard: (cardId: string) => void;
}

/** What a draft that cannot go ahead says, from the server's reason codes. */
function infeasibleText(draft: DraftResult): string {
  const reasons = draft.reasons.map((code) => S.errors.byCode[code] ?? S.errors.unknown);
  return `${S.mandates.notFeasible}${reasons.join("；")}`;
}

function reservedUntil(mandate: MandateView): string | null {
  const ends = mandate.protection.flatMap((period) => (period.until ? [period.until] : []));
  return ends.length === 0 ? null : formatDate(ends.reduce((a, b) => (a > b ? a : b)));
}

/** Agreements and the merchants' debit requests; every change and payment goes through a trusted card. */
export function MandatesPanel({ refreshKey, onChange, onCard }: Props) {
  const [total, setTotal] = useState<number | null>(null);
  const [mandates, setMandates] = useState<MandateView[]>([]);
  const [requests, setRequests] = useState<DebitRequestView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ key: string; text: string } | null>(null);

  const load = useCallback(() => {
    Promise.all([api.mandates(), api.debitRequests()])
      .then(([agreements, incoming]) => {
        setTotal(agreements.monthly_total_fen);
        setMandates(agreements.mandates);
        setRequests(incoming.requests);
        setLoaded(true);
        setMessage((current) => (current?.key === "load" ? null : current));
      })
      .catch((e: unknown) => setMessage({ key: "load", text: errorText(e) }));
  }, []);

  useEffect(load, [load, refreshKey]);

  /** Draft, then open the card in the tray; a refused or infeasible draft is shown at its row. */
  const open = async (key: string, draftCall: () => Promise<DraftResult>): Promise<boolean> => {
    setBusy(true);
    setMessage(null);
    try {
      const draft = await draftCall();
      if (!draft.feasible) {
        setMessage({ key, text: infeasibleText(draft) });
        return false;
      }
      onCard(await openCard(draft));
      onChange();
      return true;
    } catch (e) {
      setMessage({ key, text: errorText(e) });
      return false;
    } finally {
      setBusy(false);
    }
  };

  const change = (mandate: MandateView, kind: MandateChange, capFen?: number) =>
    open(mandate.mandate_ref, () => api.draftMandateChange(mandate.mandate_ref, kind, capFen));
  const decide = (request: DebitRequestView, decision: DebitDecision) =>
    open(request.request_id, () => api.draftDebitDecision(request.request_id, decision));

  return (
    <section className="panel" aria-label={S.mandates.title} data-testid="mandates-panel">
      <h2>{S.mandates.title}</h2>
      <p className="muted small">{S.mandates.subtitle}</p>
      {total !== null && (
        <p data-testid="mandates-total">
          {S.mandates.monthly} <Money fen={total} exact />
        </p>
      )}
      {message?.key === "load" && <p className="error">{message.text}</p>}
      {loaded && mandates.length === 0 && <p className="muted">{S.mandates.none}</p>}
      <ul className="mandate-list">
        {mandates.map((mandate) => (
          <MandateRow
            key={mandate.mandate_ref}
            mandate={mandate}
            busy={busy}
            message={message?.key === mandate.mandate_ref ? message.text : null}
            onChange={change}
          />
        ))}
      </ul>
      <h3>{S.mandates.requests}</h3>
      {loaded && requests.length === 0 && <p className="muted">{S.mandates.noRequests}</p>}
      <ul className="mandate-list">
        {requests.map((request) => (
          <RequestRow
            key={request.request_id}
            request={request}
            busy={busy}
            message={message?.key === request.request_id ? message.text : null}
            onDecide={decide}
          />
        ))}
      </ul>
    </section>
  );
}

interface MandateRowProps {
  mandate: MandateView;
  busy: boolean;
  message: string | null;
  onChange: (mandate: MandateView, kind: MandateChange, capFen?: number) => Promise<boolean>;
}

function MandateRow({ mandate, busy, message, onChange }: MandateRowProps) {
  const [editing, setEditing] = useState(false);
  const [cap, setCap] = useState("");
  const [error, setError] = useState<string | null>(null);
  const live = mandate.status === "ACTIVE" || mandate.status === "SUSPENDED";
  const reserved = reservedUntil(mandate);

  const submitCap = async (event: FormEvent) => {
    event.preventDefault();
    const fen = parseYuanToFen(cap);
    if (fen === null) {
      setError(S.mandates.capBad);
    } else if (fen === mandate.cap_fen) {
      setError(S.mandates.capSame);
    } else {
      setError(null);
      if (await onChange(mandate, "set_cap", fen)) {
        setEditing(false);
        setCap("");
      }
    }
  };

  return (
    <li className="mandate-row" data-testid="mandate-row" data-mandate={mandate.mandate_ref}>
      <div className="mandate-head">
        <strong>{mandate.merchant_display}</strong>
        <span className={`tag mandate-${mandate.status.toLowerCase()}`} data-testid="mandate-status">
          {S.mandates.statusNames[mandate.status]}
        </span>
      </div>
      <dl className="card-lines">
        <div>
          <dt>{S.mandates.expected}</dt>
          <dd>
            <Money fen={mandate.expected_fen} exact />
          </dd>
        </div>
        <div>
          <dt>{S.mandates.cap}</dt>
          <dd>
            <Money fen={mandate.cap_fen} exact />
          </dd>
        </div>
        {mandate.next_debit_date ? (
          <div>
            <dt>{S.mandates.nextDebit}</dt>
            <dd>{mandate.next_debit_date}</dd>
          </div>
        ) : (
          reserved && (
            <div>
              <dt>{S.mandates.reservedUntil}</dt>
              <dd>{reserved}</dd>
            </div>
          )
        )}
      </dl>
      {mandate.pending_requests > 0 && <p className="small">{S.mandates.pending(mandate.pending_requests)}</p>}
      {live && (
        <div className="actions">
          {mandate.status === "ACTIVE" && (
            <button type="button" className="secondary" disabled={busy} onClick={() => onChange(mandate, "cancel")} data-testid="mandate-cancel">
              {S.mandates.cancel}
            </button>
          )}
          {mandate.status === "ACTIVE" && (
            <button type="button" className="secondary" disabled={busy} onClick={() => onChange(mandate, "suspend")} data-testid="mandate-suspend">
              {S.mandates.suspend}
            </button>
          )}
          {mandate.status === "SUSPENDED" && (
            <button type="button" className="secondary" disabled={busy} onClick={() => onChange(mandate, "resume")} data-testid="mandate-resume">
              {S.mandates.resume}
            </button>
          )}
          {mandate.status === "SUSPENDED" && (
            <button type="button" className="secondary" disabled={busy} onClick={() => onChange(mandate, "cancel")} data-testid="mandate-cancel">
              {S.mandates.cancel}
            </button>
          )}
          <button type="button" className="secondary" disabled={busy} onClick={() => setEditing((on) => !on)} data-testid="mandate-cap">
            {editing ? S.mandates.capClose : S.mandates.changeCap}
          </button>
        </div>
      )}
      {live && editing && (
        <form onSubmit={submitCap} className="form inline-form">
          <label>
            {S.mandates.capInput}
            <input inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} disabled={busy} data-testid="mandate-cap-input" />
          </label>
          <button type="submit" disabled={busy} data-testid="mandate-cap-submit">
            {S.mandates.capSubmit}
          </button>
        </form>
      )}
      {(error ?? message) && (
        <p className="error" role="alert">
          {error ?? message}
        </p>
      )}
    </li>
  );
}

interface RequestRowProps {
  request: DebitRequestView;
  busy: boolean;
  message: string | null;
  onDecide: (request: DebitRequestView, decision: DebitDecision) => Promise<boolean>;
}

function RequestRow({ request, busy, message, onDecide }: RequestRowProps) {
  const waiting = request.status === "AWAITING_CLIENT";
  return (
    <li className="mandate-row" data-testid="debit-request" data-request={request.request_id}>
      <div className="mandate-head">
        <strong>{request.merchant_display}</strong>
        <Money fen={request.amount_fen} exact />
        <span className={`tag request-${request.status.toLowerCase()}`} data-testid="debit-status">
          {S.mandates.requestStatusNames[request.status]}
        </span>
      </div>
      {request.reason_code && (
        <p className="small" data-testid="debit-reason">
          {S.mandates.reasonNames[request.reason_code]}
        </p>
      )}
      {waiting && <p className="small">{S.mandates.respondBy(formatTime(request.respond_by))}</p>}
      {request.description_raw && (
        <div className="merchant-text" data-testid="debit-text">
          <span className="muted small">{S.mandates.textLabel}</span>
          <p>{request.description_raw}</p>
        </div>
      )}
      {request.status === "OUT_OF_MANDATE" && request.reason_code === "CAP" && (
        <p className="small" data-testid="debit-cap-hint">
          {S.mandates.capHint}
        </p>
      )}
      {request.payable && (
        <div className="actions">
          <button type="button" disabled={busy} onClick={() => onDecide(request, "pay")} data-testid="debit-pay">
            {S.mandates.pay}
          </button>
          <button type="button" className="secondary" disabled={busy} onClick={() => onDecide(request, "decline")} data-testid="debit-decline">
            {S.mandates.decline}
          </button>
        </div>
      )}
      {message && (
        <p className="error" role="alert">
          {message}
        </p>
      )}
    </li>
  );
}
