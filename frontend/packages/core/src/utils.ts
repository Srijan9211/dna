import type { EntityId } from './interfaces';

/**
 * Core utility functions
 */

/**
 * Formats a date string to a readable format (in UTC)
 */
export function formatDate(dateString: string): string {
  const date = new Date(dateString);
  return date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/**
 * Validates if a string is a valid URL
 */
export function isValidUrl(url: string): boolean {
  try {
    new URL(url);
    return true;
  } catch {
    return false;
  }
}

// Up to 15 digits is exactly representable as a JS number (and as an integer id).
const NUMERIC_ID = /^\d{1,15}$/;

/**
 * Read an entity id from text (a URL parameter, an input, a path segment).
 *
 * Mirrors the backend rule so both sides agree: a short run of digits is a number
 * (ShotGrid ids), anything else is kept as the string it is (AYON ids such as
 * ``"demo:5fd4ea6a..."``).  Returns null for empty input.
 */
export function parseEntityId(value: string | null | undefined): EntityId | null {
  const text = (value ?? '').trim();
  if (!text) return null;
  if (NUMERIC_ID.test(text)) {
    const id = Number(text);
    return id > 0 ? id : null;
  }
  return text;
}

/**
 * Coerce an untyped value (a parsed JSON payload, a stored row) to an entity id, or
 * null if it cannot be one.  Numbers must be positive integers; text goes through
 * {@link parseEntityId}.
 */
export function coerceEntityId(value: unknown): EntityId | null {
  if (typeof value === 'number') {
    return Number.isInteger(value) && value > 0 ? value : null;
  }
  if (typeof value === 'string') return parseEntityId(value);
  return null;
}

/**
 * Make a version's media URL loadable by the browser.
 *
 * The backend returns a *relative* proxy URL (``/media/versions/<id>/thumbnail?token=...``)
 * for trackers whose media needs credentials a browser cannot send (AYON), because it
 * does not know where the page is served from.  Those are resolved against the API
 * base.  Absolute URLs (ShotGrid's signed thumbnails) are returned unchanged.
 */
export function resolveMediaUrl(
  url: string | null | undefined,
  apiBase: string
): string | undefined {
  if (!url) return undefined;
  if (!url.startsWith('/media/')) return url;
  return `${apiBase.replace(/\/+$/, '')}${url}`;
}

