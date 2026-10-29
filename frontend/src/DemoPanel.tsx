import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, setCsrfToken } from "./api.ts";
import { errorText } from "./errors.ts";
import { formatTime } from "./format.ts";
import { S } from "./strings.ts";
import type { ClockView, FaultName } from "./types.ts";

const FAULTS: FaultName[] = ["drop_response", "bank_ack_lost", "crash_after_commit", "crash_before_commit"];
const VARIANTS = ["t64", "t65", "t52", "t25", "t23", "t35", "t42", "t43"];
const DAY_MS = 24 * 60 * 60 * 1000;
const HEALTH_POLL_MS = 1000;

/** The next instant after `now` at which something is due, from the plans and their operations. */
async function nextDueInstant(now: Date): Promise<Date | null> {
  const due: Date[] = [];
  const list = await api.plans();
  const plans = await Promise.all(list.plans.map((summary) => api.plan(summary.plan_id)));
  for (const plan of plans) {
    const open = plan.status !== "completed" && plan.status !== "partial";
    if (open && plan.root_approval_id && plan.expires_at) due.push(new Date(plan.expires_at));
    for (const node of plan.nodes) {
      if (open && node.status === "DRAFT" && node.not_before) due.push(new Date(node.not_before));
      if ((node.status === "SUBMITTED" || node.status === "UNKNOWN") && node.operation_id) {
        const operation = await api.operation(node.operation_id);
        if (operation.settle_due_at) due.push(new Date(operation.settle_due_at));
      }
    }
  }
  const later = due.filter((instant) => instant.getTime() > now.getTime());
  return later.length === 0 ? null : new Date(Math.min(...later.map((instant) => instant.getTime())));
}

type Login = "unknown" | "needed" | "in";

interface Props {
  refreshKey: number;
  onChange: () => void;
}

/** Demo controls: simulated clock, fault arming, a real process crash and a data reset. */
export function DemoPanel({ refreshKey, onChange }: Props) {
  const [login, setLogin] = useState<Login>("unknown");
  const [clock, setClock] = useState<ClockView | null>(null);
  const [due, setDue] = useState<Date | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [restored, setRestored] = useState(false);

  const refresh = useCallback(
    () =>
      api
        .simClock()
        .then(async (now) => {
          setClock(now);
          setDue(await nextDueInstant(new Date(now.now)));
          setError(null);
        })
        .catch((e: unknown) => setError(errorText(e))),
    [],
  );

  useEffect(() => {
    api
      .demoSession()
      .then((view) => {
        const entry = view.sessions.find((s) => s.role === "demo");
        if (entry) setCsrfToken(entry.csrf_token, "demo");
        setLogin(entry ? "in" : "needed");
      })
      .catch(() => setLogin("needed"));
  }, []);

  useEffect(() => {
    if (login === "in") refresh();
  }, [login, refresh, refreshKey]);

  // After a crash the server is down until the supervisor starts it again; poll until it answers.
  useEffect(() => {
    if (!reconnecting) return;
    const timer = window.setInterval(() => {
      api
        .health()
        .then(() => {
          setReconnecting(false);
          setRestored(true);
          onChange();
        })
        .catch(() => undefined);
    }, HEALTH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reconnecting, onChange]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await action();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const advanceTo = (target: Date) =>
    run(async () => {
      setClock(await api.simAdvanceTo(target.toISOString()));
      onChange();
      await refresh();
    });

  const crash = () =>
    run(async () => {
      setRestored(false);
      setReconnecting(true);
      try {
        await api.simCrash();
      } catch {
        // The process exits without answering; the connection closing is the expected result.
      }
    });

  if (login === "unknown") return null;
  return (
    <section className="panel demo" aria-label={S.demo.title} data-testid="demo-panel">
      {(reconnecting || restored) && (
        <div className={reconnecting ? "banner banner-down" : "banner banner-up"} role="status" data-testid="reconnect-banner">
          {reconnecting ? S.demo.reconnecting : S.demo.reconnected}
        </div>
      )}
      <h2>{S.demo.title}</h2>
      <p className="muted small">{S.demo.note}</p>
      {login === "needed" ? (
        <DemoLogin onLogin={() => setLogin("in")} />
      ) : (
        <>
          <p className="sim-clock" data-testid="sim-clock">
            <span>{S.demo.clock}</span>{" "}
            <strong data-testid="sim-clock-value">{clock ? formatTime(clock.now) : S.demo.clockUnknown}</strong>
            {clock && <span className="tag gap">{S.demo.day(clock.day)}</span>}
          </p>
          <div className="demo-group">
            <button type="button" onClick={() => due && advanceTo(due)} disabled={busy || !due} data-testid="advance-next">
              {S.demo.nextDue}
            </button>
            <p className="muted small">{due ? S.demo.nextDueAt(formatTime(due.toISOString())) : S.demo.nothingDue}</p>
          </div>
          {clock && <AdvanceToDay clock={clock} busy={busy} onAdvance={advanceTo} />}
          <div className="demo-group">
            <h4>{S.demo.faults}</h4>
            <div className="actions">
              {FAULTS.map((fault) => (
                <button
                  key={fault}
                  type="button"
                  className="secondary"
                  disabled={busy}
                  data-testid={`fault-${fault}`}
                  onClick={() =>
                    run(async () => {
                      await api.simFault(fault, 1);
                      setMessage(S.demo.faultArmed(S.demo.faultNames[fault] ?? fault));
                    })
                  }
                >
                  {S.demo.faultNames[fault] ?? fault}
                </button>
              ))}
            </div>
          </div>
          <div className="demo-group">
            <button type="button" className="danger" onClick={crash} disabled={busy || reconnecting} data-testid="crash">
              {S.demo.crash}
            </button>
            <p className="muted small">{S.demo.crashNote}</p>
          </div>
          <ResetControl
            busy={busy}
            onReset={(variant) =>
              run(async () => {
                const result = await api.simReset(variant);
                setClock(result.clock);
                setMessage(S.demo.resetDone);
                onChange();
                await refresh();
              })
            }
          />
        </>
      )}
      {message && (
        <p className="muted" data-testid="demo-message">
          {message}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function DemoLogin({ onLogin }: { onLogin: () => void }) {
  const [handle, setHandle] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const entry = await api.demoLogin(handle.trim(), password);
      if (entry.role !== "demo") {
        setError(S.errors.badCredentials);
        return;
      }
      setCsrfToken(entry.csrf_token, "demo");
      onLogin();
    } catch (e) {
      setError(errorText(e));
    }
  };

  return (
    <form onSubmit={submit} className="form" data-testid="demo-login">
      <h4>{S.demo.login}</h4>
      <label>
        {S.demo.handle}
        <input value={handle} onChange={(e) => setHandle(e.target.value)} autoComplete="off" required />
      </label>
      <label>
        {S.demo.password}
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off" required />
      </label>
      <button type="submit">{S.demo.submit}</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function AdvanceToDay({ clock, busy, onAdvance }: { clock: ClockView; busy: boolean; onAdvance: (target: Date) => void }) {
  const [day, setDay] = useState("");
  const [error, setError] = useState<string | null>(null);

  const go = (event: FormEvent) => {
    event.preventDefault();
    const n = Number(day);
    if (!/^[0-9]{1,3}$/.test(day) || n < clock.day) {
      setError(S.demo.dayInvalid);
      return;
    }
    setError(null);
    // T+n is 09:00 on the n-th day after the epoch, which is itself at 09:00.
    onAdvance(new Date(new Date(clock.epoch).getTime() + n * DAY_MS));
  };

  return (
    <form onSubmit={go} className="demo-group inline" data-testid="advance-day-form">
      <label>
        {S.demo.toDay}
        <input
          inputMode="numeric"
          value={day}
          onChange={(e) => setDay(e.target.value.replace(/\D/g, ""))}
          data-testid="advance-day-input"
        />
      </label>
      <button type="submit" disabled={busy || day === ""} data-testid="advance-day">
        {S.demo.toDayGo}
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function ResetControl({ busy, onReset }: { busy: boolean; onReset: (variant: string | null) => void }) {
  const [variant, setVariant] = useState("");
  const [confirming, setConfirming] = useState(false);

  return (
    <div className="demo-group">
      <label>
        {S.demo.variant}
        <select value={variant} onChange={(e) => setVariant(e.target.value)} data-testid="reset-variant">
          <option value="">{S.demo.variantNone}</option>
          {VARIANTS.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </select>
      </label>
      {confirming ? (
        <div className="notice">
          <p>{S.demo.resetWarning}</p>
          <button
            type="button"
            className="danger"
            disabled={busy}
            data-testid="reset-yes"
            onClick={() => {
              setConfirming(false);
              onReset(variant || null);
            }}
          >
            {S.demo.resetConfirm}
          </button>
        </div>
      ) : (
        <button type="button" className="secondary" disabled={busy} onClick={() => setConfirming(true)} data-testid="reset">
          {S.demo.reset}
        </button>
      )}
    </div>
  );
}
