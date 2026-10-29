import { ApiError } from "./errors.ts";
import type {
  ApprovalRequestResult,
  CancelResult,
  Card,
  CashSnapshotView,
  ChallengeView,
  ChatMessageView,
  ChatReply,
  ConfirmResult,
  DraftResult,
  ErrorBody,
  OperationView,
  PayeeView,
  ReceiptView,
  SessionEntry,
  SessionView,
  SmsMessage,
  VerifyResult,
} from "./types.ts";

let csrfToken = "";
let onSessionExpired: () => void = () => undefined;

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

export function setSessionExpiredHandler(handler: () => void): void {
  onSessionExpired = handler;
}

async function request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method === "POST") {
    headers["X-CSRF-Token"] = csrfToken;
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
    if (error.code === "SESSION_EXPIRED" && error.detail["reason"] !== "bad_credentials") {
      onSessionExpired();
    }
    throw error;
  }
  return parsed as T;
}

const get = <T>(path: string) => request<T>("GET", path);
const post = <T>(path: string, body?: unknown) => request<T>("POST", path, body);

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

  sendChat: (text: string) => post<ChatReply>("/v1/chat/messages", { text }),
  chatHistory: () => get<{ messages: ChatMessageView[] }>("/v1/chat/history?limit=50"),
};
