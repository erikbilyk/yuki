import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { parseYuanToFen } from "./money.ts";
import { S } from "./strings.ts";
import type { MandateView, SimDebitBody, SimDebitResult } from "./types.ts";

// A request from a merchant the bank has no agreement with; the server declines it as NO_MANDATE.
const UNREGISTERED = "unregistered";
const UNREGISTERED_MERCHANT = "mer_unregistered";
const UNREGISTERED_MANDATE = "mdt_unregistered";

interface Props {
  refreshKey: number;
  onChange: () => void;
}

/** Plays a merchant: sends a debit request for one of the customer's agreements, or repeats the last one. */
export function DebitRequestForm({ refreshKey, onChange }: Props) {
  const [mandates, setMandates] = useState<MandateView[]>([]);
  const [choice, setChoice] = useState("");
  const [amount, setAmount] = useState("");
  const [text, setText] = useState("");
  const [last, setLast] = useState<SimDebitBody | null>(null);
  const [result, setResult] = useState<SimDebitResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(0);

  // Until the customer picks, the first agreement is selected.
  const mandateRef = choice || mandates[0]?.mandate_ref || UNREGISTERED;

  const load = useCallback(() => {
    api
      .mandates()
      .then((view) => setMandates(view.mandates))
      .catch((e: unknown) => setError(errorText(e)));
  }, []);
  useEffect(load, [load, refreshKey]);

  const send = async (body: SimDebitBody) => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.simDebitRequest(body));
      setLast(body);
      onChange();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const fen = parseYuanToFen(amount);
    if (fen === null) {
      setError(S.demo.debit.badAmount);
      return;
    }
    const mandate = mandates.find((m) => m.mandate_ref === mandateRef);
    setSent((n) => n + 1);
    void send({
      merchant_id: mandate?.merchant_id ?? UNREGISTERED_MERCHANT,
      merchant_request_id: `ui-${Date.now().toString(36)}-${sent + 1}`,
      mandate_ref: mandate?.mandate_ref ?? UNREGISTERED_MANDATE,
      amount_fen: fen,
      currency: "CNY",
      description_raw: text.trim(),
    });
  };

  return (
    <form onSubmit={submit} className="demo-group form" data-testid="sim-debit-form">
      <h4>{S.demo.debit.title}</h4>
      <label>
        {S.demo.debit.mandate}
        <select value={mandateRef} onChange={(e) => setChoice(e.target.value)} data-testid="sim-debit-mandate">
          {mandates.map((m) => (
            <option key={m.mandate_ref} value={m.mandate_ref}>
              {m.merchant_display}
            </option>
          ))}
          <option value={UNREGISTERED}>{S.demo.debit.unregistered}</option>
        </select>
      </label>
      <label>
        {S.demo.debit.amount}
        <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} data-testid="sim-debit-amount" />
      </label>
      <label>
        {S.demo.debit.text}
        <input maxLength={140} value={text} onChange={(e) => setText(e.target.value)} data-testid="sim-debit-text" />
      </label>
      <div className="actions">
        <button type="submit" disabled={busy} data-testid="sim-debit-send">
          {S.demo.debit.send}
        </button>
        <button type="button" className="secondary" disabled={busy || last === null} onClick={() => last && send(last)} data-testid="sim-debit-repeat">
          {S.demo.debit.repeat}
        </button>
      </div>
      {result && (
        <p className="muted small" data-testid="sim-debit-result" data-duplicate={result.duplicate}>
          {S.demo.debit.result(
            S.mandates.requestStatusNames[result.status],
            result.reason_code ? S.mandates.reasonNames[result.reason_code] : null,
            result.duplicate,
          )}
          {" "}
          {S.mandates.respondBy(formatTime(result.respond_by))}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
