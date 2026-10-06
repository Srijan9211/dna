import { describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_CAPABILITIES,
  DEFAULT_LABELS,
  deriveWsUrl,
  fetchPublicConfig,
  labelFor,
  resolveConfig,
  type BuildEnv,
  type PublicConfig,
} from './runtimeConfig';

const NO_ENV: BuildEnv = {};

describe('resolveConfig: precedence is server, then build-time, then default', () => {
  it('with no server config behaves exactly as the build-time env always did', () => {
    const env: BuildEnv = {
      VITE_AUTH_PROVIDER: 'shotgrid',
      VITE_GOOGLE_CLIENT_ID: 'gid',
      VITE_FEATURE_AI: 'false',
      VITE_FEATURE_TRANSCRIPTION: 'true',
      VITE_WS_URL: 'wss://x/ws',
      VITE_PRODTRACK_TAB_SYNC_EXTENSION_ID: '  ext  ',
      VITE_DEV_MODE: 'true',
    };
    const c = resolveConfig(null, env);
    expect(c.source).toBe('build');
    expect(c.authProvider).toBe('shotgrid');
    expect(c.googleClientId).toBe('gid');
    expect(c.features).toEqual({ transcription: true, inReview: null, ai: false });
    expect(c.wsUrl).toBe('wss://x/ws');
    expect(c.tabSyncExtensionId).toBe('ext');
    expect(c.devMode).toBe(true);
    expect(c.tracker.displayName).toBe('ShotGrid');
  });

  it('defaults to google when nothing says otherwise (the historic default)', () => {
    expect(resolveConfig(null, NO_ENV).authProvider).toBe('google');
    expect(resolveConfig(null, { VITE_AUTH_PROVIDER: 'bogus' }).authProvider).toBe('google');
  });

  it("the server's login mode wins: a build-time value that disagrees could never work", () => {
    const c = resolveConfig({ auth: { provider: 'ayon' } }, { VITE_AUTH_PROVIDER: 'shotgrid' });
    expect(c.authProvider).toBe('ayon');
    expect(c.source).toBe('server');
    expect(c.tracker.displayName).toBe('AYON');
  });

  it('ignores an unknown server login mode instead of breaking the app', () => {
    const c = resolveConfig({ auth: { provider: 'kerberos' } }, { VITE_AUTH_PROVIDER: 'none' });
    expect(c.authProvider).toBe('none');
  });

  it('uses the server for the Google client id, else the build', () => {
    expect(resolveConfig({ auth: { provider: 'google', google_client_id: 'srv' } }, { VITE_GOOGLE_CLIENT_ID: 'build' }).googleClientId).toBe('srv');
    expect(resolveConfig({ auth: { provider: 'google' } }, { VITE_GOOGLE_CLIENT_ID: 'build' }).googleClientId).toBe('build');
  });

  it('a server feature flag overrides the build one; null means "no override"', () => {
    const server: PublicConfig = { frontend: { features: { ai: false, transcription: null, in_review: true } } };
    const c = resolveConfig(server, { VITE_FEATURE_AI: 'true', VITE_FEATURE_TRANSCRIPTION: 'false' });
    expect(c.features).toEqual({ ai: false, transcription: false, inReview: true });
  });

  it('prefers an explicit websocket URL, then derives one from the API base, then falls back', () => {
    expect(resolveConfig({ frontend: { ws_url: 'wss://srv/ws' } }, { VITE_WS_URL: 'wss://b/ws' }).wsUrl).toBe('wss://srv/ws');
    expect(resolveConfig(null, { VITE_WS_URL: 'wss://b/ws' }).wsUrl).toBe('wss://b/ws');
    expect(resolveConfig(null, { VITE_API_BASE_URL: 'https://dna.example/' }).wsUrl).toBe('wss://dna.example/ws');
    expect(resolveConfig(null, NO_ENV).wsUrl).toBe('ws://localhost:8000/ws');
  });

  it('merges the tracker vocabulary over the defaults', () => {
    const c = resolveConfig(
      { prodtrack: { connector: 'ayon', display_name: 'AYON', labels: { playlist: 'List' }, capabilities: { note_addressing: false, note_mentions: true } } },
      NO_ENV
    );
    expect(c.tracker.labels.playlist).toBe('List');
    expect(c.tracker.labels.shot).toBe(DEFAULT_LABELS.shot); // untouched labels stay
    expect(c.tracker.capabilities.note_addressing).toBe(false);
    expect(c.tracker.capabilities.playlists).toBe(DEFAULT_CAPABILITIES.playlists);
    expect(c.tracker.connector).toBe('ayon');
  });

  it('tolerates a sparse server response (an older backend)', () => {
    const c = resolveConfig({}, { VITE_AUTH_PROVIDER: 'none' });
    expect(c.authProvider).toBe('none');
    expect(c.tracker.labels).toEqual(DEFAULT_LABELS);
    expect(c.configVersion).toBeNull();
  });
});

describe('deriveWsUrl', () => {
  it.each([
    ['https://dna.example', 'wss://dna.example/ws'],
    ['http://localhost:8000', 'ws://localhost:8000/ws'],
    ['https://dna.example/', 'wss://dna.example/ws'],
    ['', null],
    ['/api', null],
  ])('%s -> %s', (input, expected) => {
    expect(deriveWsUrl(input)).toBe(expected);
  });
});

describe('labelFor', () => {
  const config = resolveConfig({ prodtrack: { labels: { playlist: 'List' } } }, NO_ENV);

  it('uses the tracker word, whatever the case of the kind', () => {
    expect(labelFor(config, 'playlist')).toBe('List');
    expect(labelFor(config, 'Playlist')).toBe('List');
    expect(labelFor(config, 'Shot')).toBe('Shot');
  });

  it('falls back to the kind itself for anything unknown', () => {
    expect(labelFor(config, 'Sequence')).toBe('Sequence');
  });
});

describe('fetchPublicConfig', () => {
  const ok = (body: unknown) =>
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(body) });

  it('reads /config/public relative to the API base', async () => {
    const fetchImpl = ok({ auth: { provider: 'ayon' } });
    const result = await fetchPublicConfig('https://api.example/', { fetchImpl });
    expect(result).toEqual({ auth: { provider: 'ayon' } });
    expect(fetchImpl.mock.calls[0][0]).toBe('https://api.example/config/public');
  });

  it('works with a same-origin (empty) base', async () => {
    const fetchImpl = ok({});
    await fetchPublicConfig('', { fetchImpl });
    expect(fetchImpl.mock.calls[0][0]).toBe('/config/public');
  });

  it.each([
    ['a backend without the endpoint (404)', vi.fn().mockResolvedValue({ ok: false, status: 404 })],
    ['a network error', vi.fn().mockRejectedValue(new TypeError('network'))],
    ['malformed JSON', vi.fn().mockResolvedValue({ ok: true, json: () => Promise.reject(new SyntaxError('x')) })],
    ['a non-object body', ok('nope')],
    ['a null body', ok(null)],
  ])('returns null for %s, so the app falls back to build-time values', async (_name, fetchImpl) => {
    expect(await fetchPublicConfig('', { fetchImpl })).toBeNull();
  });

  it('gives up after the timeout instead of hanging the app', async () => {
    const fetchImpl = vi.fn(
      (_url: unknown, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        })
    ) as unknown as typeof fetch;
    expect(await fetchPublicConfig('', { fetchImpl, timeoutMs: 20 })).toBeNull();
  });
});
