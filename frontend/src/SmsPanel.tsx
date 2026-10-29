import { useCallback, useEffect, useState } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { useInterval } from "./hooks.ts";
import { Money } from "./Money.tsx";
import { S } from "./strings.ts";
import type { SmsMessage } from "./types.ts";

const REFRESH_MS = 3000;

export function SmsPanel({ refreshKey }: { refreshKey: number }) {
  const [messages, setMessages] = useState<SmsMessage[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .sms()
      .then((view) => {
        setMessages(view.messages);
        setError(null);
      })
      .catch((e: unknown) => setError(errorText(e)));
  }, []);

  useEffect(load, [load, refreshKey]);
  useInterval(load, REFRESH_MS);

  return (
    <section className="panel sms" aria-label={S.sms.title}>
      <h2>{S.sms.title}</h2>
      <p className="muted small">{S.sms.note}</p>
      {error && <p className="error">{error}</p>}
      {messages.length === 0 ? (
        <p className="muted">{S.sms.empty}</p>
      ) : (
        <ul className="sms-list">
          {messages.map((m) => (
            <li key={m.challenge_id} data-testid="sms-message">
              <div className="sms-head">
                {S.kinds[m.kind]}
                {m.amount_fen !== null && (
                  <>
                    {" · "}
                    <Money fen={m.amount_fen} />
                  </>
                )}
              </div>
              <div className="sms-code" data-testid="sms-code">
                {m.code}
              </div>
              <div className="muted small">
                {S.sms.expires} {formatTime(m.expires_at)}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
