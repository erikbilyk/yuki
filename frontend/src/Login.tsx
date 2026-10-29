import { useState } from "react";
import type { FormEvent } from "react";
import { api, setCsrfToken } from "./api.ts";
import { errorText } from "./errors.ts";
import { S } from "./strings.ts";
import type { SessionEntry } from "./types.ts";

export function Login({ onLogin }: { onLogin: (entry: SessionEntry) => void }) {
  const [handle, setHandle] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const entry = await api.login(handle.trim(), password);
      if (entry.role !== "customer") {
        // The cookie of another role is harmless here; the customer screens need the customer's.
        setError(S.login.notCustomer);
        return;
      }
      setCsrfToken(entry.csrf_token);
      onLogin(entry);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <form onSubmit={submit} className="panel form" data-testid="login-form">
        <h1>{S.appName}</h1>
        <h2>{S.login.title}</h2>
        <label>
          {S.login.handle}
          <input value={handle} onChange={(e) => setHandle(e.target.value)} autoComplete="username" required />
        </label>
        <label>
          {S.login.password}
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? S.login.submitting : S.login.submit}
        </button>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </form>
    </main>
  );
}
