import { useCallback, useEffect, useState } from "react";
import { api, setCsrfToken, setSessionExpiredHandler } from "./api.ts";
import { CardFlow } from "./CardFlow.tsx";
import { AuditView } from "./AuditView.tsx";
import { CashPanel } from "./CashPanel.tsx";
import { ChatPanel } from "./ChatPanel.tsx";
import { DemoPanel } from "./DemoPanel.tsx";
import { GuidedForms } from "./GuidedForms.tsx";
import { Login } from "./Login.tsx";
import { PlanPanel } from "./PlanPanel.tsx";
import { SmsPanel } from "./SmsPanel.tsx";
import { S } from "./strings.ts";
import type { SessionEntry } from "./types.ts";

const MAX_TRACKED_CARDS = 5;
const CARD_PATH = /^\/cards\/([A-Za-z0-9_-]+)\/?$/;

function storageKey(subject: string): string {
  return `wg.cards.${subject}`;
}

function loadTracked(subject: string): string[] {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(storageKey(subject)) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

export function App() {
  const [session, setSession] = useState<SessionEntry | null | undefined>(undefined);
  const [demo, setDemo] = useState(false);

  useEffect(() => {
    setSessionExpiredHandler(() => setSession(null));
    api
      .health()
      .then((health) => setDemo(health.demo))
      .catch(() => setDemo(false));
    api
      .session()
      .then((view) => {
        const customer = view.sessions.find((entry) => entry.role === "customer") ?? null;
        if (customer) setCsrfToken(customer.csrf_token);
        setSession(customer);
      })
      .catch(() => setSession(null));
  }, []);

  if (session === undefined) {
    return <p className="muted center">{S.loading}</p>;
  }
  if (session === null) {
    return <Login onLogin={setSession} />;
  }
  return <Workspace key={session.subject_id} session={session} demo={demo} onLogout={() => setSession(null)} />;
}

function Workspace({ session, demo, onLogout }: { session: SessionEntry; demo: boolean; onLogout: () => void }) {
  const subject = session.subject_id;
  const [refreshKey, setRefreshKey] = useState(0);
  const [auditMode, setAuditMode] = useState(false);
  const [cards, setCards] = useState<string[]>(() => {
    const opened = CARD_PATH.exec(window.location.pathname)?.[1];
    const stored = loadTracked(subject);
    return opened && !stored.includes(opened) ? [opened, ...stored] : stored;
  });

  useEffect(() => {
    if (CARD_PATH.test(window.location.pathname)) window.history.replaceState(null, "", "/");
  }, []);

  useEffect(() => {
    window.localStorage.setItem(storageKey(subject), JSON.stringify(cards.slice(0, MAX_TRACKED_CARDS)));
  }, [cards, subject]);

  const bump = useCallback(() => setRefreshKey((k) => k + 1), []);
  const addCards = useCallback((ids: string[]) => {
    setCards((current) => [...ids, ...current.filter((id) => !ids.includes(id))].slice(0, MAX_TRACKED_CARDS));
  }, []);
  const closeCard = useCallback((id: string) => setCards((current) => current.filter((c) => c !== id)), []);

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      onLogout();
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <strong>{S.appName}</strong>
        <span className="who">
          <button type="button" className="link" onClick={() => setAuditMode((on) => !on)} data-testid="audit-toggle">
            {auditMode ? S.audit.close : S.audit.open}
          </button>
          {session.display_name}
          <button type="button" className="link" onClick={logout}>
            {S.logout}
          </button>
        </span>
      </header>
      {auditMode ? (
        <main className="layout layout-single">
          <AuditView refreshKey={refreshKey} />
        </main>
      ) : (
        <main className="layout">
          <ChatPanel onCards={addCards} onChange={bump} />
          <aside className="side">
            <CashPanel refreshKey={refreshKey} />
            {cards.length > 0 && (
              <div className="tray" aria-label={S.card.title}>
                <h2>{S.card.title}</h2>
                {cards.map((id) => (
                  <CardFlow key={id} cardId={id} onChange={bump} onClose={closeCard} />
                ))}
              </div>
            )}
            <PlanPanel refreshKey={refreshKey} onChange={bump} onCard={(id) => addCards([id])} />
            <SmsPanel refreshKey={refreshKey} />
            <GuidedForms onCard={(id) => addCards([id])} />
            {demo && <DemoPanel refreshKey={refreshKey} onChange={bump} />}
          </aside>
        </main>
      )}
    </div>
  );
}
