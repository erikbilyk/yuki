import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { parseYuanToFen } from "./money.ts";
import { S } from "./strings.ts";
import type { DraftResult, PayeeView } from "./types.ts";

interface Props {
  onCard: (cardId: string) => void;
}

function reasonText(code: string): string {
  return S.errors.byCode[code] ?? S.errors.unknown;
}

/** Fill the draft route's form fields, then ask for the trusted card; nothing executes here. */
async function openCard(draft: DraftResult): Promise<string> {
  const requested = await api.requestApproval(draft.logical_action_id);
  return requested.card_id;
}

export function GuidedForms({ onCard }: Props) {
  const [payees, setPayees] = useState<PayeeView[]>([]);
  const [payeeId, setPayeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [remark, setRemark] = useState("");
  const [reserve, setReserve] = useState("");
  const [busy, setBusy] = useState<"transfer" | "reserve" | null>(null);
  const [message, setMessage] = useState<{ form: "transfer" | "reserve"; text: string } | null>(null);

  useEffect(() => {
    api
      .payees()
      .then((view) => setPayees(view.payees))
      .catch((e: unknown) => setMessage({ form: "transfer", text: errorText(e) }));
  }, []);

  const run = async (form: "transfer" | "reserve", draftCall: () => Promise<DraftResult>) => {
    setBusy(form);
    setMessage(null);
    try {
      const draft = await draftCall();
      if (!draft.feasible) {
        setMessage({ form, text: `${S.guided.notFeasible}${draft.reasons.map(reasonText).join("；")}` });
        return;
      }
      onCard(await openCard(draft));
      if (form === "transfer") {
        setAmount("");
        setRemark("");
      } else {
        setReserve("");
      }
    } catch (e) {
      setMessage({ form, text: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  const submitTransfer = (event: FormEvent) => {
    event.preventDefault();
    const fen = parseYuanToFen(amount);
    if (!payeeId) {
      setMessage({ form: "transfer", text: S.guided.choosePayee });
    } else if (fen === null) {
      setMessage({ form: "transfer", text: S.guided.badAmount });
    } else {
      void run("transfer", () => api.draftTransfer(payeeId, fen, remark.trim() || null));
    }
  };

  const submitReserve = (event: FormEvent) => {
    event.preventDefault();
    // A target of zero is allowed for the reserve, unlike a payment amount.
    const fen = reserve.trim() === "0" ? 0 : parseYuanToFen(reserve);
    if (fen === null) {
      setMessage({ form: "reserve", text: S.guided.badAmount });
    } else {
      void run("reserve", () => api.draftReserve(fen));
    }
  };

  return (
    <section className="panel" aria-label={S.guided.title}>
      <h2>{S.guided.title}</h2>
      <form onSubmit={submitTransfer} className="form" data-testid="transfer-form">
        <h3>{S.guided.transfer}</h3>
        <label>
          {S.guided.payee}
          <select value={payeeId} onChange={(e) => setPayeeId(e.target.value)} disabled={busy !== null}>
            <option value="">—</option>
            {payees.map((p) => (
              <option key={p.payee_id} value={p.payee_id}>
                {p.display_name}（{p.account_masked}）
              </option>
            ))}
          </select>
        </label>
        <label>
          {S.guided.amount}
          <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} disabled={busy !== null} />
        </label>
        <label>
          {S.guided.remark}
          <input maxLength={64} value={remark} onChange={(e) => setRemark(e.target.value)} disabled={busy !== null} />
        </label>
        <button type="submit" disabled={busy !== null}>
          {busy === "transfer" ? S.guided.preparing : S.guided.prepare}
        </button>
        {message?.form === "transfer" && (
          <p className="error" role="alert">
            {message.text}
          </p>
        )}
      </form>
      <form onSubmit={submitReserve} className="form" data-testid="reserve-form">
        <h3>{S.guided.reserve}</h3>
        <label>
          {S.guided.reserveTarget}
          <input inputMode="decimal" value={reserve} onChange={(e) => setReserve(e.target.value)} disabled={busy !== null} />
        </label>
        <button type="submit" disabled={busy !== null}>
          {busy === "reserve" ? S.guided.preparing : S.guided.reserveSubmit}
        </button>
        {message?.form === "reserve" && (
          <p className="error" role="alert">
            {message.text}
          </p>
        )}
      </form>
    </section>
  );
}
