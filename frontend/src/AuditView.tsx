import { useCallback, useEffect, useState } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { Money } from "./Money.tsx";
import { S } from "./strings.ts";
import type { AuditLine, PlanView } from "./types.ts";

/** The server's plain line for an event; a few plan events fall back to this screen's own wording. */
function lineText(line: AuditLine): string {
  return S.audit.events[line.event] ?? line.text;
}

/** Audit mode for judges: every recorded step in time order, and the revisions with their reasons. */
export function AuditView({ refreshKey }: { refreshKey: number }) {
  const [plans, setPlans] = useState<PlanView[]>([]);
  const [planId, setPlanId] = useState<string | null>(null);
  const [lines, setLines] = useState<AuditLine[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([
      api.plans().then((list) => Promise.all(list.plans.map((summary) => api.plan(summary.plan_id)))),
      api.audit(planId),
    ])
      .then(([views, audit]) => {
        setPlans(views);
        // The server lists newest first; judges read oldest first.
        setLines([...audit.lines].reverse());
        setError(null);
      })
      .catch((e: unknown) => setError(errorText(e)));
  }, [planId]);

  useEffect(load, [load, refreshKey]);

  const shown = planId ? plans.filter((plan) => plan.plan_id === planId) : plans;
  return (
    <section className="panel audit" aria-label={S.audit.title} data-testid="audit-view">
      <h2>{S.audit.title}</h2>
      <p className="muted small">{S.audit.note}</p>
      <label>
        {S.audit.planFilter}
        <select value={planId ?? ""} onChange={(e) => setPlanId(e.target.value || null)} data-testid="audit-plan-filter">
          <option value="">{S.audit.allPlans}</option>
          {plans.map((plan) => (
            <option key={plan.plan_id} value={plan.plan_id}>
              {S.plan.kinds[plan.kind] ?? S.plan.title} · {formatTime(plan.created_at)}
            </option>
          ))}
        </select>
      </label>
      {error && <p className="error">{error}</p>}
      {shown.some((plan) => plan.revisions.length > 0) && (
        <div className="audit-revisions" data-testid="audit-revisions">
          <h3>{S.audit.revisionsTitle}</h3>
          <ul>
            {shown.flatMap((plan) =>
              plan.revisions.map((revision) => (
                <li key={`${plan.plan_id}-${revision.revision}`} data-testid="audit-revision">
                  {S.audit.revisionLine(
                    revision.revision,
                    S.plan.revisionReasons[revision.reason],
                    revision.within_bounds ? S.plan.withinBounds : S.plan.outsideBounds,
                    plan.root_approval_id ?? S.audit.revisionNoApproval,
                  )}
                  {revision.purchase_before_fen !== null && revision.purchase_after_fen !== null && (
                    <div className="small">
                      {S.plan.purchaseChange} <Money fen={revision.purchase_before_fen} /> → <Money fen={revision.purchase_after_fen} />
                    </div>
                  )}
                </li>
              )),
            )}
          </ul>
        </div>
      )}
      {lines.length === 0 && !error ? (
        <p className="muted">{S.audit.empty}</p>
      ) : (
        <table className="audit-table">
          <thead>
            <tr>
              <th>{S.audit.time}</th>
              <th>{S.audit.actor}</th>
              <th>{S.audit.event}</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line, index) => (
              <tr key={index} data-testid="audit-line" data-event={line.event}>
                <td className="muted small">{formatTime(line.at)}</td>
                <td className="muted small">{S.audit.actors[line.actor] ?? line.actor}</td>
                <td>{lineText(line)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
