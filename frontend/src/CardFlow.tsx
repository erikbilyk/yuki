import { useCallback, useEffect, useState } from "react";
import { api } from "./api.ts";
import { ApprovalCard } from "./ApprovalCard.tsx";
import { CodeEntry } from "./CodeEntry.tsx";
import { errorCode, errorText } from "./errors.ts";
import { OperationStatus } from "./OperationStatus.tsx";
import { S } from "./strings.ts";
import type { ActionStatus, Card, ChallengeView, OperationView } from "./types.ts";

interface Props {
  cardId: string;
  onChange: () => void;
  onClose: (cardId: string) => void;
}

type Phase =
  | { name: "card" }
  | { name: "code"; challenge: ChallengeView | null }
  | { name: "done"; status: ActionStatus | null; operation: OperationView | null };

/** One trusted card from request to receipt: confirm, code entry, operation status. */
export function CardFlow({ cardId, onChange, onClose }: Props) {
  const [card, setCard] = useState<Card | null>(null);
  const [phase, setPhase] = useState<Phase>({ name: "card" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyCard = useCallback((loaded: Card) => {
    setCard(loaded);
    if (loaded.status === "confirmed") {
      setPhase(loaded.requires_code ? { name: "code", challenge: null } : { name: "done", status: null, operation: null });
    }
  }, []);

  const loadFailed = useCallback(
    (e: unknown) => {
      setError(errorText(e));
      if (errorCode(e) === "NOT_FOUND") onClose(cardId);
    },
    [cardId, onClose],
  );

  useEffect(() => {
    api.card(cardId).then(applyCard).catch(loadFailed);
  }, [cardId, applyCard, loadFailed]);

  if (!card) {
    return (
      <section className="panel" data-testid="card-flow">
        {error ? <p className="error">{error}</p> : <p className="muted">{S.loading}</p>}
      </section>
    );
  }

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      // The hash is the one of the card on screen; a changed card is refused by the server.
      const result = await api.confirm(card.card_id, card.terms_hash);
      onChange();
      if (result.challenge) {
        setPhase({ name: "code", challenge: result.challenge });
      } else if (card.requires_code && !result.operation) {
        setPhase({ name: "code", challenge: null });
      } else {
        setPhase({ name: "done", status: result.action_status, operation: result.operation });
      }
      setCard({ ...card, status: "confirmed", approval_id: result.approval_id });
    } catch (e) {
      setError(errorText(e));
      if (errorCode(e) === "TERMS_MISMATCH") {
        await api.card(cardId).then(applyCard).catch(loadFailed);
        setError(S.card.termsChanged);
      }
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!card.logical_action_id) return;
    setBusy(true);
    setError(null);
    try {
      await api.cancelAction(card.logical_action_id);
      onChange();
      onClose(card.card_id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel flow" data-testid="card-flow">
      <ApprovalCard card={card} />
      {card.status !== "open" && (
        <p className="muted small" data-testid="card-status">
          {S.card.statusNames[card.status]}
        </p>
      )}
      {phase.name === "card" && card.status === "open" && (
        <div className="actions">
          <button type="button" onClick={confirm} disabled={busy} data-testid="confirm">
            {busy ? S.card.confirming : S.card.confirm}
          </button>
          {card.logical_action_id && (
            <button type="button" className="secondary" onClick={cancel} disabled={busy}>
              {S.card.cancel}
            </button>
          )}
        </div>
      )}
      {phase.name === "code" && card.logical_action_id && (
        <CodeEntry
          actionId={card.logical_action_id}
          challenge={phase.challenge}
          onChallenge={(challenge) => setPhase({ name: "code", challenge })}
          onVerified={(result) => {
            onChange();
            setPhase({ name: "done", status: result.action_status, operation: result.operation });
          }}
          onStop={cancel}
        />
      )}
      {phase.name === "done" && phase.operation && <OperationStatus initial={phase.operation} onChange={onChange} />}
      {phase.name === "done" && !phase.operation && phase.status && (
        <p className="status" data-testid="action-status">
          {S.statuses[phase.status]}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {(phase.name === "done" || card.status !== "open") && (
        <button type="button" className="link" onClick={() => onClose(card.card_id)}>
          {S.card.dismiss}
        </button>
      )}
    </section>
  );
}
