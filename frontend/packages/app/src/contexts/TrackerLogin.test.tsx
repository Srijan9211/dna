import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { type ReactNode } from 'react';
import { configure, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ThemeProvider } from 'styled-components';
import { Theme } from '@radix-ui/themes';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { theme } from '../styles';
import { AuthProvider, isSessionAuth, useAuth } from './AuthContext';
import { RuntimeConfigProvider } from './RuntimeConfigContext';
import { ThemeModeProvider } from './ThemeContext';
import { TrackerLoginPage, loginCopy } from '../components/ShotGridLoginPage';

// A tiny backend: /config/public says which tracker, /auth/* answers like the real one.
function backend(opts: { provider: 'ayon' | 'shotgrid'; loginOk?: boolean }) {
  const calls: { url: string; body?: unknown }[] = [];
  const respond = (status: number, body: unknown) =>
    Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(body),
    });
  const user =
    opts.provider === 'ayon'
      ? { id: 'bob', email: 'bob@x.com', name: 'Bob Sup' }
      : { id: 42, email: 'bob@x.com', name: 'Bob Sup', shotgrid_user_id: 42 };

  const impl = vi.fn((url: string, init?: RequestInit) => {
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    calls.push({ url: path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (path === '/config/public') {
      return respond(200, {
        auth: { provider: opts.provider },
        prodtrack: {
          connector: opts.provider,
          display_name: opts.provider === 'ayon' ? 'AYON' : 'ShotGrid',
        },
      });
    }
    if (path === '/auth/refresh') return respond(401, { detail: 'No refresh token' });
    if (path === '/auth/login') {
      return opts.loginOk === false
        ? respond(401, { detail: 'Invalid username or password.' })
        : respond(200, { access_token: 'jwt', expires_in: 900, user });
    }
    return respond(404, {});
  });
  return { impl, calls };
}

function Gate() {
  const { authProvider, isAuthenticated, isLoading, user } = useAuth();
  // Mirrors App.tsx: the form stays mounted while a sign-in is in flight, so an
  // error from a refused login is still on screen when it settles.
  if (isSessionAuth(authProvider) && (isLoading || !isAuthenticated)) {
    return <TrackerLoginPage />;
  }
  return <div>signed in as {user?.email} via {authProvider}</div>;
}

function app(): ReactNode {
  return (
    <RuntimeConfigProvider env={{}}>
      <QueryClientProvider client={new QueryClient()}>
        <ThemeProvider theme={theme}>
          <Theme>
            <ThemeModeProvider>
              <AuthProvider>
                <Gate />
              </AuthProvider>
            </ThemeModeProvider>
          </Theme>
        </ThemeProvider>
      </QueryClientProvider>
    </RuntimeConfigProvider>
  );
}

// These tests run the real providers and a mocked network; under a loaded full-suite
// run the default 1s is too tight for the first render of the Radix tree.
configure({ asyncUtilTimeout: 8000 });

/**
 * The form's inputs are disabled while the initial "is there a session to restore?"
 * check is in flight, so a test must wait for them to be usable before typing.
 */
async function signIn(
  user: ReturnType<typeof userEvent.setup>,
  labels: { username: string; password: string },
  credentials: { username: string; password: string }
) {
  const username = await screen.findByLabelText(labels.username);
  await waitFor(() => expect(username).toBeEnabled());
  await user.type(username, credentials.username);
  await user.type(screen.getByPlaceholderText(labels.password), credentials.password);
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('signing in with AYON', () => {
  it('shows an AYON-worded form chosen by the server, with no ShotGrid wording', async () => {
    const { impl } = backend({ provider: 'ayon' });
    vi.stubGlobal('fetch', impl);
    render(app());

    expect(await screen.findByText('Sign in with your AYON credentials')).toBeInTheDocument();
    expect(screen.getByText('User name or email')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('AYON password')).toBeInTheDocument();
    expect(screen.queryByText(/Legacy/i)).toBeNull();
    expect(screen.queryByText(/ShotGrid/i)).toBeNull();
  });

  it('signs in through /auth/login and ends up authenticated as that user', async () => {
    const { impl, calls } = backend({ provider: 'ayon' });
    vi.stubGlobal('fetch', impl);
    const user = userEvent.setup();
    render(app());

    await signIn(user, { username: 'User name or email', password: 'AYON password' }, { username: 'bob', password: 'right' });

    expect(await screen.findByText(/signed in as bob@x.com via ayon/)).toBeInTheDocument();
    expect(calls.find((c) => c.url === '/auth/login')?.body).toEqual({
      username: 'bob',
      password: 'right',
    });
    // the AYON user's id is their (string) username, kept as is
    expect(JSON.parse(sessionStorage.getItem('dna-sg-user') ?? '{}').id).toBe('bob');
  });

  it("shows AYON's refusal and stays on the form", async () => {
    const { impl } = backend({ provider: 'ayon', loginOk: false });
    vi.stubGlobal('fetch', impl);
    const user = userEvent.setup();
    render(app());

    await signIn(user, { username: 'User name or email', password: 'AYON password' }, { username: 'bob', password: 'wrong' });

    expect(await screen.findByText('Invalid username or password.')).toBeInTheDocument();
    expect(screen.getByText('Sign in with your AYON credentials')).toBeInTheDocument();
  });

  it('never keeps the password anywhere in the browser', async () => {
    const { impl } = backend({ provider: 'ayon' });
    vi.stubGlobal('fetch', impl);
    const user = userEvent.setup();
    render(app());
    await signIn(user, { username: 'User name or email', password: 'AYON password' }, { username: 'bob', password: 'hunter2-secret' });
    await screen.findByText(/signed in as/);

    const stored = JSON.stringify({ ...sessionStorage, ...localStorage });
    expect(stored).not.toContain('hunter2-secret');
  });
});

describe('ShotGrid sign-in is unchanged', () => {
  it('keeps its Legacy Password wording and PAT help', async () => {
    vi.stubGlobal('fetch', backend({ provider: 'shotgrid' }).impl);
    render(app());
    expect(await screen.findByText('Sign in with your ShotGrid credentials')).toBeInTheDocument();
    expect(screen.getByText('Email / Username')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('ShotGrid Legacy Password')).toBeInTheDocument();
    expect(screen.getByText(/Cloud ShotGrid:/)).toBeInTheDocument();
  });

  it('still signs in and keeps its numeric id', async () => {
    vi.stubGlobal('fetch', backend({ provider: 'shotgrid' }).impl);
    const user = userEvent.setup();
    render(app());
    await signIn(user, { username: 'Email / Username', password: 'ShotGrid Legacy Password' }, { username: 'bob', password: 'pw' });
    expect(await screen.findByText(/signed in as bob@x.com via shotgrid/)).toBeInTheDocument();
    await waitFor(() => expect(JSON.parse(sessionStorage.getItem('dna-sg-user') ?? '{}').id).toBe(42));
  });
});

describe('loginCopy', () => {
  it('words each tracker separately', () => {
    expect(loginCopy('ayon', 'AYON').passwordPlaceholder).toBe('AYON password');
    expect(loginCopy('shotgrid', 'ShotGrid').passwordPlaceholder).toBe('ShotGrid Legacy Password');
  });
});

describe('isSessionAuth', () => {
  it('is true for the username + password trackers only', () => {
    expect(isSessionAuth('ayon')).toBe(true);
    expect(isSessionAuth('shotgrid')).toBe(true);
    expect(isSessionAuth('google')).toBe(false);
    expect(isSessionAuth('none')).toBe(false);
  });
});
