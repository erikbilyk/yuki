import { ApiError } from "./errors.ts";
import type {
  ApprovalRequestResult,
  CancelResult,
  Card,
  CashSnapshotView,
  ChallengeView,
  ChatMessageView,
  ChatReply,
  ClockView,
  ConfirmResult,
  DraftResult,
  ErrorBody,
  FaultName,
  FaultView,
  AuditLine,
  HealthView,
  OperationView,
  PayeeView,
  PlanSummary,
  PlanView,
  ReceiptView,
  SessionEntry,
  SessionView,
  SimResetResult,
  SmsMessage,
  VerifyResult,
} from "./types.ts";

type Role = "customer" | "demo";

// Each role has its own session cookie and CSRF token, so the demo panel runs beside the customer screen.
const csrfTokens: Record<Role, string> = { customer: "", demo: "" };
let onSessionExpired: () => void = () => undefined;

export function setCsrfToken(token: string, role: Role = "customer"): void {
  csrfTokens[role] = token;
}

export function setSessionExpiredHandler(handler: () => void): void {
  onSessionExpired = handler;
}

async function request<T>(method: "GET" | "POST", path: string, body?: unknown, role: Role = "customer"): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method === "POST") {
    headers["X-CSRF-Token"] = csrfTokens[role];
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const parsed: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new ApiError(response.status, parsed as Partial<ErrorBody> | null);
    if (role === "customer" && error.code === "SESSION_EXPIRED" && error.detail["reason"] !== "bad_credentials") {
      onSessionExpired();
    }
    throw error;
  }
  return parsed as T;
}

const get = <T>(path: string) => request<T>("GET", path);
const post = <T>(path: string, body?: unknown) => request<T>("POST", path, body);
const sim = <T>(path: string, body?: unknown) => request<T>("POST", path, body, "demo");

export const api = {
  session: () => get<SessionView>("/v1/session"),
  login: (handle: string, password: string) =>
    post<SessionEntry>("/v1/auth/login", { handle, password }),
  logout: () => post<SessionView>("/v1/auth/logout", { role: "customer" }),

  snapshot: () => get<CashSnapshotView>("/v1/cash/snapshot"),
  payees: () => get<{ payees: PayeeView[] }>("/v1/payees"),
  accounts: () => get<{ accounts: { account_id: string; name: string }[] }>("/v1/accounts"),
  operation: (id: string) => get<OperationView>(`/v1/operations/${encodeURIComponent(id)}`),
  receipt: (operationId: string) =>
    get<ReceiptView>(`/v1/receipts/${encodeURIComponent(operationId)}`),

  draftTransfer: (payeeId: string, amountFen: number, remark: string | null) =>
    post<DraftResult>("/v1/drafts/transfer", {
      payee_id: payeeId,
      amount_fen: amountFen,
      ...(remark ? { remark } : {}),
    }),
  draftReserve: (targetFen: number) =>
    post<DraftResult>("/v1/drafts/reserve", { target_fen: targetFen }),
  requestApproval: (actionId: string) =>
    post<ApprovalRequestResult>(`/v1/actions/${encodeURIComponent(actionId)}/request-approval`),
  cancelAction: (actionId: string) =>
    post<CancelResult>(`/v1/actions/${encodeURIComponent(actionId)}/cancel`),

  card: (id: string) => get<Card>(`/v1/cards/${encodeURIComponent(id)}`),
  confirm: (cardId: string, termsHash: string) =>
    post<ConfirmResult>(`/v1/cards/${encodeURIComponent(cardId)}/confirm`, {
      terms_hash: termsHash,
    }),
  challenge: (actionId: string) =>
    post<ChallengeView>(`/v1/actions/${encodeURIComponent(actionId)}/challenge`),
  reissue: (challengeId: string) =>
    post<ChallengeView>(`/v1/challenges/${encodeURIComponent(challengeId)}/reissue`),
  verify: (challengeId: string, code: string) =>
    post<VerifyResult>(`/v1/challenges/${encodeURIComponent(challengeId)}/verify`, { code }),
  sms: () => get<{ messages: SmsMessage[] }>("/v1/sms"),

  plans: () => get<{ plans: PlanSummary[] }>("/v1/plans"),
  plan: (id: string) => get<PlanView>(`/v1/plans/${encodeURIComponent(id)}`),
  stopPlan: (id: string) => post<PlanView>(`/v1/plans/${encodeURIComponent(id)}/stop`),
  audit: (planId: string | null) =>
    get<{ lines: AuditLine[] }>(`/v1/audit?limit=500${planId ? `&plan_id=${encodeURIComponent(planId)}` : ""}`),

  health: () => get<HealthView>("/healthz"),
  demoSession: () => get<SessionView>("/v1/session"),
  demoLogin: (handle: string, password: string) =>
    request<SessionEntry>("POST", "/v1/auth/login", { handle, password }, "demo"),
  simClock: () => sim<ClockView>("/sim/clock/advance", { days: 0, minutes: 0 }),
  simAdvanceTo: (to: string) => sim<ClockView>("/sim/clock/advance", { to }),
  simAdvanceDays: (days: number) => sim<ClockView>("/sim/clock/advance", { days }),
  simFault: (fault: FaultName, count: number) =>
    sim<{ faults: FaultView[] }>("/sim/faults", { fault, count }),
  simReset: (variant: string | null) =>
    sim<SimResetResult>("/sim/reset", { profile: "fixture_v1", ...(variant ? { variant } : {}) }),
  simCrash: () => sim<null>("/sim/crash"),

  sendChat: (text: string) => post<ChatReply>("/v1/chat/messages", { text }),
  chatHistory: () => get<{ messages: ChatMessageView[] }>("/v1/chat/history?limit=50"),
};
