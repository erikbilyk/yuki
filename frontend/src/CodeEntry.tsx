import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api.ts";
import { attemptsLeft, errorCode, errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { S } from "./strings.ts";
import type { ChallengeView, VerifyResult } from "./types.ts";

const REISSUE_CODES = new Set(["CODE_EXPIRED", "CODE_ATTEMPTS_EXHAUSTED", "APPROVAL_REQUIRED", "SUPERSEDED"]);

interface Props {
  actionId: string;
  challenge: ChallengeView | null;
  onChallenge: (challenge: ChallengeView) => void;
  onVerified: (result: VerifyResult) => void;
  onStop: () => void;
  /** Plan steps are stopped with the plan, so their code entry has no stop of its own. */
  hideStop?: boolean;
}

/** Code entry outside the chat. The code is typed here and goes only to the verify route. */
export function CodeEntry({ actionId, challenge, onChallenge, onVerified, onStop, hideStop = false }: Props) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needsNew, setNeedsNew] = useState(challenge === null);
  const [left, setLeft] = useState<number | null>(challenge?.attempts_left ?? null);

  const issue = async () => {
    setBusy(true);
    setError(null);
    try {
      const issued = challenge ? await api.reissue(challenge.challenge_id) : await api.challenge(actionId);
      onChallenge(issued);
      setLeft(issued.attempts_left);
      setNeedsNew(false);
      setCode("");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!challenge) {
      return;
    }
    if (!/^[0-9]{6}$/.test(code)) {
      setError(S.code.badFormat);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onVerified(await api.verify(challenge.challenge_id, code));
    } catch (e) {
      setError(errorText(e));
      setNeedsNew(REISSUE_CODES.has(errorCode(e) ?? ""));
      if (errorCode(e) === "CODE_INVALID") {
        const reported = attemptsLeft(e);
        setLeft((current) => reported ?? (current === null ? null : Math.max(0, current - 1)));
      }
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="code-entry" aria-label={S.code.title} data-testid="code-entry">
      <h4>{S.code.title}</h4>
      {challenge && !needsNew ? (
        <form onSubmit={submit}>
          <p className="muted small">{S.code.hint}</p>
          <label>
            {S.code.input}
            <input
              className="code-input"
              inputMode="numeric"
              autoComplete="off"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              disabled={busy}
              data-testid="code-input"
            />
          </label>
          <p className="muted small">
            {S.code.expires} {formatTime(challenge.expires_at)}
            {left !== null && <span className="gap">{S.code.attemptsLeft(left)}</span>}
          </p>
          <button type="submit" disabled={busy || code.length !== 6}>
            {busy ? S.code.submitting : S.code.submit}
          </button>
        </form>
      ) : (
        <button type="button" onClick={issue} disabled={busy}>
          {challenge ? S.code.reissue : S.code.request}
        </button>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!hideStop && (
        <button type="button" className="link" onClick={onStop} disabled={busy}>
          {S.code.stop}
        </button>
      )}
    </section>
  );
}
