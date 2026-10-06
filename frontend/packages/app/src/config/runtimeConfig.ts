/**
 * Runtime configuration.
 *
 * What used to be baked into the build as `VITE_*` variables is now read from the
 * backend at boot (`GET /config/public`), so one image serves every deployment and
 * a studio changes the tracker, login mode or features without a rebuild.
 *
 * Precedence for every setting: the server's value, else the legacy build-time
 * `VITE_*` value, else the app's own default.  A backend that predates
 * `/config/public` (or is unreachable at boot) therefore behaves exactly as before.
 */
import type { EntityType } from '@dna/core';

export type AuthProviderType = 'none' | 'google' | 'shotgrid' | 'ayon';

/** The tracker vocabulary DNA shows users, by DNA kind. */
export type KindLabels = Record<string, string>;

export interface TrackerCapabilities {
  playlists: boolean;
  create_playlist: boolean;
  add_to_playlist: boolean;
  notes: boolean;
  note_attachments: boolean;
  note_addressing: boolean;
  note_mentions: boolean;
  version_status_write: boolean;
  author_attribution: boolean;
  transcript_storage: string[];
  impersonation: string;
}

/** Shape of `GET /config/public` (all fields optional: older backends send less). */
export interface PublicConfig {
  config_version?: number;
  auth?: { provider?: string; google_client_id?: string | null };
  features?: { transcript_publish?: boolean };
  frontend?: {
    features?: {
      transcription?: boolean | null;
      in_review?: boolean | null;
      ai?: boolean | null;
    };
    ws_url?: string | null;
    prodtrack_tab_sync_extension_id?: string | null;
    dev_mode?: boolean;
  };
  prodtrack?: {
    connector?: string;
    display_name?: string;
    labels?: KindLabels;
    roles?: Record<string, string[]>;
    capabilities?: Partial<TrackerCapabilities>;
  };
}

/** The slice of `import.meta.env` this module reads. */
export interface BuildEnv {
  VITE_API_BASE_URL?: string;
  VITE_AUTH_PROVIDER?: string;
  VITE_GOOGLE_CLIENT_ID?: string;
  VITE_FEATURE_TRANSCRIPTION?: string;
  VITE_FEATURE_IN_REVIEW?: string;
  VITE_FEATURE_AI?: string;
  VITE_WS_URL?: string;
  VITE_PRODTRACK_TAB_SYNC_EXTENSION_ID?: string;
  VITE_DEV_MODE?: string;
}

export interface ResolvedConfig {
  /** Where the effective values came from. */
  source: 'server' | 'build';
  configVersion: number | null;
  apiBase: string;
  authProvider: AuthProviderType;
  googleClientId: string;
  /** null = no override: the app uses the user's own saved choice or its default. */
  features: {
    transcription: boolean | null;
    inReview: boolean | null;
    ai: boolean | null;
  };
  wsUrl: string;
  tabSyncExtensionId: string;
  devMode: boolean;
  tracker: {
    connector: string;
    displayName: string;
    labels: KindLabels;
    roles: Record<string, string[]>;
    capabilities: TrackerCapabilities;
  };
}

// The words the UI has always used; a tracker's profile overrides them.
export const DEFAULT_LABELS: KindLabels = {
  project: 'Project',
  shot: 'Shot',
  asset: 'Asset',
  task: 'Task',
  version: 'Version',
  playlist: 'Playlist',
  user: 'User',
  note: 'Note',
};

export const DEFAULT_CAPABILITIES: TrackerCapabilities = {
  playlists: true,
  create_playlist: true,
  add_to_playlist: true,
  notes: true,
  note_attachments: true,
  note_addressing: true,
  note_mentions: false,
  version_status_write: true,
  author_attribution: true,
  transcript_storage: ['dna_only'],
  impersonation: 'none',
};

const AUTH_PROVIDERS: readonly string[] = ['none', 'google', 'shotgrid', 'ayon'];
const FALLBACK_WS_URL = 'ws://localhost:8000/ws';

function asAuthProvider(value: string | undefined | null): AuthProviderType | null {
  return value && AUTH_PROVIDERS.includes(value)
    ? (value as AuthProviderType)
    : null;
}

function readBool(value: string | undefined): boolean | null {
  if (value === 'true') return true;
  if (value === 'false') return false;
  return null;
}

/** `https://dna.example` -> `wss://dna.example/ws`; null if it cannot be derived. */
export function deriveWsUrl(apiBase: string): string | null {
  const base = apiBase.trim().replace(/\/+$/, '');
  if (/^https:\/\//i.test(base)) return `${base.replace(/^https/i, 'wss')}/ws`;
  if (/^http:\/\//i.test(base)) return `${base.replace(/^http/i, 'ws')}/ws`;
  return null;
}

function defaultTrackerName(auth: AuthProviderType): string {
  if (auth === 'shotgrid') return 'ShotGrid';
  if (auth === 'ayon') return 'AYON';
  return 'Production tracker';
}

/** Merge the server's public config (may be null) over the build-time environment. */
export function resolveConfig(
  server: PublicConfig | null,
  env: BuildEnv
): ResolvedConfig {
  const apiBase = env.VITE_API_BASE_URL ?? '';
  const serverAuth = asAuthProvider(server?.auth?.provider);
  // The server is authoritative on how it authenticates: a build-time value that
  // disagrees with it could never work.
  const authProvider: AuthProviderType =
    serverAuth ?? asAuthProvider(env.VITE_AUTH_PROVIDER) ?? 'google';

  const frontend = server?.frontend;
  const pt = server?.prodtrack;
  const hasServer = server !== null;

  return {
    source: hasServer ? 'server' : 'build',
    configVersion: server?.config_version ?? null,
    apiBase,
    authProvider,
    googleClientId:
      server?.auth?.google_client_id || env.VITE_GOOGLE_CLIENT_ID || '',
    features: {
      transcription:
        frontend?.features?.transcription ??
        readBool(env.VITE_FEATURE_TRANSCRIPTION),
      inReview:
        frontend?.features?.in_review ?? readBool(env.VITE_FEATURE_IN_REVIEW),
      ai: frontend?.features?.ai ?? readBool(env.VITE_FEATURE_AI),
    },
    wsUrl:
      frontend?.ws_url ||
      env.VITE_WS_URL ||
      deriveWsUrl(apiBase) ||
      FALLBACK_WS_URL,
    tabSyncExtensionId: (
      frontend?.prodtrack_tab_sync_extension_id ??
      env.VITE_PRODTRACK_TAB_SYNC_EXTENSION_ID ??
      ''
    ).trim(),
    devMode: frontend?.dev_mode ?? env.VITE_DEV_MODE === 'true',
    tracker: {
      connector: pt?.connector ?? authProvider,
      displayName: pt?.display_name || defaultTrackerName(authProvider),
      labels: { ...DEFAULT_LABELS, ...(pt?.labels ?? {}) },
      roles: pt?.roles ?? {},
      capabilities: { ...DEFAULT_CAPABILITIES, ...(pt?.capabilities ?? {}) },
    },
  };
}

export interface FetchOptions {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

/**
 * Read `/config/public`.  Returns null on any failure (network, timeout, a backend
 * without the endpoint, malformed JSON): the caller falls back to build-time values.
 */
export async function fetchPublicConfig(
  apiBase: string,
  { fetchImpl = fetch, timeoutMs = 4000 }: FetchOptions = {}
): Promise<PublicConfig | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(`${apiBase.replace(/\/+$/, '')}/config/public`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    return body !== null && typeof body === 'object' ? (body as PublicConfig) : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** The tracker's word for a DNA kind ("collection" -> "List" on AYON). */
export function labelFor(
  config: Pick<ResolvedConfig, 'tracker'>,
  kind: string
): string {
  return config.tracker.labels[kind.toLowerCase()] ?? kind;
}

/** Map a DNA entity type name ("Playlist") to its profile label. */
export function labelForType(
  config: Pick<ResolvedConfig, 'tracker'>,
  type: EntityType | string
): string {
  return labelFor(config, type);
}
