import { useCallback, useEffect, useState } from "react";
import { api } from "./api.ts";
import { CodeEntry } from "./CodeEntry.tsx";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { useInterval } from "./hooks.ts";
import { Money, Percent } from "./Money.tsx";
import { pauseReasonText, nodeSentence } from "./planText.ts";
import { S } from "./strings.ts";
import type { ChallengeView, PlanNodeView, PlanView, ReceiptView } from "./types.ts";

const REFRESH_MS = 4000;

interface PanelProps {
  refreshKey: number;
  onChange: () => void;
  onCard: (cardId: string) => void;
}

/** The customer's plans as a task list: one plain sentence per step, the revisions and a stop button. */
export function PlanPanel({ refreshKey, onChange, onCard }: PanelProps) {
  const [plans, setPlans] = useState<PlanView[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .plans()
      .then((list) => Promise.all(list.plans.map((summary) => api.plan(summary.plan_id))))
      .then((views) => {
        setPlans(views);
        setError(null);
      })
      .catch((e: unknown) => setError(errorText(e)));
  }, []);

  useEffect(load, [load, refreshKey]);
  useInterval(load, REFRESH_MS);

  const changed = useCallback(() => {
    load();
    onChange();
  }, [load, onChange]);

  return (
    <section className="panel plans" aria-label={S.plan.title} data-testid="plan-panel">
      <h2>{S.plan.title}</h2>
      {error && <p className="error">{error}</p>}
      {plans.length === 0 && !error && <p className="muted">{S.plan.none}</p>}
      {plans.map((plan) => (
        <PlanBlock key={plan.plan_id} plan={plan} onChange={changed} onCard={onCard} />
      ))}
    </section>
  );
}

function PlanBlock({ plan, onChange, onCard }: { plan: PlanView; onChange: () => void; onCard: (id: string) => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stoppable = plan.status === "active" || plan.status === "paused" || plan.status === "locked";

  const stop = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.stopPlan(plan.plan_id);
      setConfirming(false);
      onChange();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="plan" data-testid="plan" data-plan-status={plan.status}>
      <header className="plan-head">
        <h3>{S.plan.kinds[plan.kind] ?? S.plan.title}</h3>
        <span className={`plan-status plan-status-${plan.status}`} data-testid="plan-status">
          {S.plan.statusNames[plan.status]}
        </span>
      </header>
      {plan.expires_at && (
        <p className="muted small">
          {S.plan.expires} {formatTime(plan.expires_at)}
        </p>
      )}
      {plan.card_id && plan.status === "draft" && (
        <button type="button" className="secondary" onClick={() => onCard(plan.card_id as string)} data-testid="open-plan-card">
          {S.plan.openCard}
        </button>
      )}
      <ol className="nodes">
        {plan.nodes.map((node) => (
          <NodeRow key={node.logical_action_id} node={node} plan={plan} onChange={onChange} />
        ))}
      </ol>
      {plan.checkpoint && <Checkpoint plan={plan} />}
      {plan.revisions.length > 1 && <Revisions plan={plan} />}
      {stoppable && !confirming && (
        <button type="button" className="secondary" onClick={() => setConfirming(true)} data-testid="plan-stop">
          {S.plan.stop}
        </button>
      )}
      {stoppable && confirming && (
        <div className="notice" data-testid="plan-stop-confirm">
          <p>{S.plan.stopWarning}</p>
          <div className="actions">
            <button type="button" onClick={stop} disabled={busy} data-testid="plan-stop-yes">
              {S.plan.stopConfirm}
            </button>
            <button type="button" className="secondary" onClick={() => setConfirming(false)} disabled={busy}>
              {S.plan.stopBack}
            </button>
          </div>
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}

function nodeTitle(node: PlanNodeView): string {
  const base = S.kinds[node.kind];
  return node.payee_display ? `${base}：${node.payee_display}` : base;
}

function NodeRow({ node, plan, onChange }: { node: PlanNodeView; plan: PlanView; onChange: () => void }) {
  const [receipt, setReceipt] = useState<ReceiptView | null>(null);
  const [showReceipt, setShowReceipt] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const waitingForCode = node.status === "AWAITING_STEPUP" && plan.status !== "draft";

  const toggleReceipt = async () => {
    if (showReceipt) {
      setShowReceipt(false);
      return;
    }
    setError(null);
    try {
      if (node.operation_id && !receipt) setReceipt(await api.receipt(node.operation_id));
      setShowReceipt(true);
    } catch (e) {
      setError(errorText(e));
    }
  };

  return (
    <li className={`node node-${node.phrase}`} data-testid="plan-node" data-node-kind={node.kind} data-node-status={node.status}>
      <div className="node-head">
        <strong>{nodeTitle(node)}</strong>
        {node.revision > 1 && <span className="tag">{S.plan.nodeRevision(node.revision)}</span>}
      </div>
      <p className="node-phrase" data-testid="node-phrase">
        {nodeSentence(node)}
      </p>
      <p className="muted small">
        {node.amount_fen !== null && (
          <>
            {S.plan.nodeAmount} <Money fen={node.amount_fen} />
          </>
        )}
        {node.shares !== null && (
          <span className="gap">
            {S.plan.nodeShares} {node.shares}
          </span>
        )}
      </p>
      {node.status === "AWAITING_STEPUP" && node.challenge_expires_at && (
        <p className="muted small">{S.plan.codeValidUntil(formatTime(node.challenge_expires_at))}</p>
      )}
      {node.status === "PAUSED" && (
        <div className="notice" data-testid="node-pause">
          <ul>
            {node.pause_reasons.map((code) => (
              <li key={code}>{pauseReasonText(code)}</li>
            ))}
          </ul>
          <p className="muted small">{S.plan.pauseNote}</p>
        </div>
      )}
      {waitingForCode && (
        <NodeCode key={`${node.logical_action_id}-${node.revision}`} actionId={node.logical_action_id} onDone={onChange} />
      )}
      {node.status === "SETTLED" && node.operation_id && (
        <>
          <button type="button" className="link" onClick={toggleReceipt} data-testid="node-receipt">
            {showReceipt ? S.plan.receiptHide : S.plan.receipt}
          </button>
          {showReceipt && receipt && <ReceiptLines receipt={receipt} />}
        </>
      )}
      {error && <p className="error">{error}</p>}
    </li>
  );
}

function NodeCode({ actionId, onDone }: { actionId: string; onDone: () => void }) {
  const [challenge, setChallenge] = useState<ChallengeView | null>(null);
  return (
    <CodeEntry
      actionId={actionId}
      challenge={challenge}
      onChallenge={setChallenge}
      onVerified={onDone}
      onStop={onDone}
      hideStop
    />
  );
}

function ReceiptLines({ receipt }: { receipt: ReceiptView }) {
  return (
    <dl className="card-lines receipt" data-testid="node-receipt-lines">
      {receipt.amount_fen !== null && (
        <div>
          <dt>{S.receipt.amount}</dt>
          <dd>
            <Money fen={receipt.amount_fen} />
          </dd>
        </div>
      )}
      {receipt.fee_fen !== null && (
        <div>
          <dt>{S.receipt.fee}</dt>
          <dd>
            <Money fen={receipt.fee_fen} />
          </dd>
        </div>
      )}
      {receipt.net_fen !== null && (
        <div>
          <dt>{S.receipt.net}</dt>
          <dd>
            <Money fen={receipt.net_fen} />
          </dd>
        </div>
      )}
      <div>
        <dt>{S.receipt.time}</dt>
        <dd>{formatTime(receipt.created_at)}</dd>
      </div>
    </dl>
  );
}

function Checkpoint({ plan }: { plan: PlanView }) {
  const checkpoint = plan.checkpoint;
  if (!checkpoint) return null;
  return (
    <div className="checkpoint" data-testid="plan-checkpoint">
      <h4>{S.plan.checkpoint}</h4>
      <p>{S.plan.checkpointResults[checkpoint.result]}</p>
      <p className="muted small">
        {S.plan.checkpointMeasured} <Percent value={checkpoint.measured_pct} quality={checkpoint.quality} />
        <span className="gap">
          {S.plan.checkpointTarget} <Percent value={checkpoint.target_max_pct} quality="confirmed" />
        </span>
      </p>
    </div>
  );
}

function Revisions({ plan }: { plan: PlanView }) {
  return (
    <div className="revisions" data-testid="plan-revisions">
      <h4>{S.plan.revisions}</h4>
      <ul>
        {plan.revisions.map((revision) => (
          <li key={revision.revision} data-testid="plan-revision">
            {S.plan.nodeRevision(revision.revision)}：{S.plan.revisionReasons[revision.reason]}
            <span className="muted small gap">{formatTime(revision.created_at)}</span>
            {revision.purchase_before_fen !== null && revision.purchase_after_fen !== null && (
              <div className="small">
                {S.plan.purchaseChange} <Money fen={revision.purchase_before_fen} /> → <Money fen={revision.purchase_after_fen} />
                <span className="gap">{revision.within_bounds ? S.plan.withinBounds : S.plan.outsideBounds}</span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
