/**
 * Turn a failed admin API call into something showable.
 *
 * The backend answers a bad change with 422 `{detail: {message, issues[]}}`, a stale
 * save with 409 `{detail: "..."}`, a refused save of a failing handshake with 409
 * `{detail: {message, report}}`, and anything else with a string `detail`.
 */
import type { ConfigIssue, HandshakeReport } from '@dna/core';

export interface AdminError {
  message: string;
  issues: ConfigIssue[];
  /** Present when a save was refused because the handshake had failing checks. */
  report: HandshakeReport | null;
  /** The change was based on an out-of-date revision: reload and retry. */
  stale: boolean;
}

interface AxiosLike {
  response?: { status?: number; data?: { detail?: unknown } };
  message?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function describeAdminError(error: unknown): AdminError {
  const fallback: AdminError = {
    message: 'Something went wrong. Please try again.',
    issues: [],
    report: null,
    stale: false,
  };
  if (!isRecord(error)) return fallback;
  const e = error as AxiosLike;
  const status = e.response?.status;
  const detail = e.response?.data?.detail;

  if (status === 401 || status === 403) {
    return {
      ...fallback,
      message:
        typeof detail === 'string' && detail
          ? detail
          : 'You are not allowed to change configuration.',
    };
  }
  if (typeof detail === 'string' && detail) {
    return { ...fallback, message: detail, stale: status === 409 };
  }
  if (isRecord(detail)) {
    const issues = Array.isArray(detail.issues)
      ? (detail.issues as ConfigIssue[])
      : [];
    const report = isRecord(detail.report)
      ? (detail.report as unknown as HandshakeReport)
      : null;
    return {
      message:
        typeof detail.message === 'string' ? detail.message : fallback.message,
      issues,
      report,
      stale: false,
    };
  }
  if (!e.response && typeof e.message === 'string' && e.message) {
    return { ...fallback, message: `Could not reach the server: ${e.message}` };
  }
  return fallback;
}

/** `{ ok: true, value }` for a JSON object, else `{ ok: false, error }`. */
export function parseJsonObject(
  text: string
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    return {
      ok: false,
      error: `Not valid JSON: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
  if (!isRecord(parsed)) {
    return { ok: false, error: 'Expected a JSON object, like {"key": "value"}.' };
  }
  return { ok: true, value: parsed };
}

/** Drop values a UI shows but must not send back: masked secrets. */
export function withoutMaskedSecrets(
  values: Record<string, unknown>
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    if (value === '********') continue;
    out[key] = isRecord(value) ? withoutMaskedSecrets(value) : value;
  }
  return out;
}
