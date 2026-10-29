import { S } from "./strings.ts";
import type { ErrorBody } from "./types.ts";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: Record<string, unknown>;

  constructor(status: number, body: Partial<ErrorBody> | null) {
    super(body?.message_zh ?? "");
    this.status = status;
    this.code = body?.code ?? "UNKNOWN";
    this.detail = body?.detail ?? {};
  }
}

/** The text for an error: the screen's wording for the code, else the server's own zh-CN message. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "SESSION_EXPIRED") {
      return error.detail["reason"] === "bad_credentials" ? S.errors.badCredentials : S.errors.sessionExpired;
    }
    return S.errors.byCode[error.code] ?? (error.message || S.errors.unknown);
  }
  if (error instanceof TypeError) {
    return S.errors.network;
  }
  return S.errors.unknown;
}

export function errorCode(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null;
}

/** Remaining code attempts from a refusal's detail, when the server names them. */
export function attemptsLeft(error: unknown): number | null {
  if (!(error instanceof ApiError)) {
    return null;
  }
  const value = error.detail["attempts_left"];
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}
