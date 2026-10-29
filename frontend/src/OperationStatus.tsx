import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { useInterval } from "./hooks.ts";
import { Money } from "./Money.tsx";
import { S } from "./strings.ts";
import type { OperationView, ReceiptView } from "./types.ts";

const POLL_MS = 2000;

function Receipt({ receipt }: { receipt: ReceiptView }) {
  const settled = receipt.status === "SETTLED";
  const rows: [string, ReactNode][] = [];
  if (receipt.amount_fen !== null) rows.push([S.receipt.amount, <Money fen={receipt.amount_fen} />]);
  if (receipt.fee_fen !== null) rows.push([S.receipt.fee, <Money fen={receipt.fee_fen} />]);
  if (receipt.net_fen !== null) rows.push([S.receipt.net, <Money fen={receipt.net_fen} />]);
  if (receipt.payee_display) rows.push([S.receipt.payee, receipt.payee_display]);
  if (receipt.unpaid_after_fen !== null) rows.push([S.receipt.unpaidAfter, <Money fen={receipt.unpaid_after_fen} />]);
  if (receipt.reason_code) rows.push([S.receipt.reason, S.errors.byCode[receipt.reason_code] ?? receipt.reason_code]);
  rows.push([S.receipt.time, formatTime(receipt.created_at)]);
  return (
    <div className={`receipt ${settled ? "receipt-ok" : "receipt-failed"}`} data-testid="receipt">
      <h4>
        {S.receipt.title}：{settled ? S.receipt.statusSettled : S.receipt.statusRejected}
      </h4>
      <dl className="card-lines">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

interface Props {
  initial: OperationView;
  onChange: () => void;
}

/** Follows one operation until the bank settles or rejects it, then shows its receipt. */
export function OperationStatus({ initial, onChange }: Props) {
  const [operation, setOperation] = useState(initial);
  const [receipt, setReceipt] = useState<ReceiptView | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);

  const final = operation.status === "SETTLED" || operation.status === "REJECTED";

  const poll = useCallback(() => {
    api
      .operation(operation.operation_id)
      .then((next) => {
        setOperation(next);
        if (next.status === "SETTLED" || next.status === "REJECTED") {
          onChange();
        }
      })
      .catch((e: unknown) => setMessage(errorText(e)));
  }, [operation.operation_id, onChange]);

  useInterval(() => {
    if (!final) poll();
  }, POLL_MS);

  useEffect(() => {
    if (!final) return;
    api
      .receipt(operation.operation_id)
      .then(setReceipt)
      .catch((e: unknown) => setMessage(errorText(e)));
  }, [final, operation.operation_id]);

  const undo = async () => {
    setUndoing(true);
    setMessage(null);
    try {
      await api.cancelAction(operation.logical_action_id);
      onChange();
    } catch (e) {
      setMessage(errorText(e));
    } finally {
      setUndoing(false);
    }
  };

  return (
    <section className="operation" aria-label={S.operation.title} data-testid="operation">
      <h4>{S.operation.title}</h4>
      <p className={`status status-${operation.status.toLowerCase()}`} data-testid="operation-status">
        {S.operation.statusNames[operation.status]}
      </p>
      <dl className="card-lines">
        <div>
          <dt>{S.kinds[operation.kind]}</dt>
          <dd>{operation.amount_fen !== null && <Money fen={operation.amount_fen} />}</dd>
        </div>
        <div>
          <dt>{S.operation.submittedAt}</dt>
          <dd>{formatTime(operation.submitted_at)}</dd>
        </div>
        {operation.settled_at && (
          <div>
            <dt>{S.operation.settledAt}</dt>
            <dd>{formatTime(operation.settled_at)}</dd>
          </div>
        )}
      </dl>
      {receipt && <Receipt receipt={receipt} />}
      {operation.status === "SETTLED" && operation.kind === "TRANSFER" && (
        <button type="button" className="link" onClick={undo} disabled={undoing}>
          {S.operation.undo}
        </button>
      )}
      {message && (
        <p className="error" role="alert">
          {message}
        </p>
      )}
    </section>
  );
}
