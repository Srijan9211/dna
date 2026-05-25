import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useRef,
  type ReactNode,
} from 'react';
import { apiHandler } from '../api';

const TOKEN_KEY = 'dna-sg-token';
const USER_KEY = 'dna-sg-user';

// JWT auto-refresh 25 minutes before expiry (token lifetime is 480 min = 8 h)
const REFRESH_INTERVAL_MS = 25 * 60 * 1000;

export type ShotGridAuthMode = 'pat' | 'sso' | 'none' | null;
export type AuthMethod = 'shotgrid_pat' | 'shotgrid_sso' | 'google' | null;

export interface ShotGridUser {
  id: number | string;
  email: string;
  name: string;
  shotgrid_user_id?: number;
  auth_mode?: AuthMethod;
}

/** Shape returned by GET /auth/login for a single mode entry */
export interface ModeConfig {
  enabled: boolean;
  redirect_url?: string;
}

/** All available login options from the backend */
export interface LoginModes {
  shotgrid_pat: ModeConfig;
  shotgrid_sso: ModeConfig;
  google: ModeConfig;
}

interface ShotGridAuthContextValue {
  isAuthenticated: boolean;
  isLoading: boolean;
  /** Parsed available login modes from the backend */
  loginModes: LoginModes;
  /** Legacy field — kept for backward compat */
  mode: ShotGridAuthMode;
  modeWarning: string | null;
  /** Error surfaced from an SSO or Google popup callback failure */
  ssoError: string | null;
  clearSsoError: () => void;
  user: ShotGridUser | null;
  token: string | null;
  authProvider: 'shotgrid';
  /** ShotGrid PAT (username + password) login */
  signIn: (username: string, password: string) => Promise<void>;
  /** Open SSO popup for ShotGrid APS or Google */
  signInWithSso: (provider: 'shotgrid_sso' | 'google') => void;
  /** Called by Google popup after obtaining the Google access token */
  signInWithGoogleToken: (googleToken: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshToken: () => Promise<void>;
}

const ShotGridAuthContext = createContext<ShotGridAuthContextValue | null>(null);

const DEFAULT_MODES: LoginModes = {
  shotgrid_pat: { enabled: true },
  shotgrid_sso: { enabled: false },
  google: { enabled: false },
};

interface ShotGridAuthProviderProps {
  children: ReactNode;
}

export function ShotGridAuthProvider({ children }: ShotGridAuthProviderProps) {
  const [loginModes, setLoginModes] = useState<LoginModes>(DEFAULT_MODES);
  const [mode, setMode] = useState<ShotGridAuthMode>(null);
  const [modeWarning, setModeWarning] = useState<string | null>(null);
  const [ssoError, setSsoError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [user, setUser] = useState<ShotGridUser | null>(() => {
    const stored = sessionStorage.getItem(USER_KEY);
    if (stored) {
      try { return JSON.parse(stored); } catch { return null; }
    }
    return null;
  });
  const [token, setToken] = useState<string | null>(
    () => sessionStorage.getItem(TOKEN_KEY)
  );

  const refreshTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

  // ── Helpers ──────────────────────────────────────────────────────────── //

  const persist = useCallback((jwt: string, authUser: ShotGridUser) => {
    sessionStorage.setItem(TOKEN_KEY, jwt);
    sessionStorage.setItem(USER_KEY, JSON.stringify(authUser));
    setToken(jwt);
    setUser(authUser);
    apiHandler.setUser({ id: String(authUser.id), email: authUser.email, name: authUser.name, token: jwt });
  }, []);

  const clear = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
    setToken(null);
    setUser(null);
    apiHandler.setUser(null);
  }, []);

  const clearSsoError = useCallback(() => setSsoError(null), []);

  // ── Popup detection ─────────────────────────────────────────────────── //
  const isInPopup = !!(window.opener && !window.opener.closed);

  // ── SSO callback handler (APS code grant → backend) ─────────────────── //

  const handleSsoCallback = useCallback(async (
    sessionToken: string | null,
    code: string | null,
    state: string | null,
  ) => {
    setIsLoading(true);
    try {
      const params = new URLSearchParams();
      if (sessionToken) params.set('session_token', sessionToken);
      if (code) params.set('code', code);
      if (state) params.set('state', state);

      const res = await fetch(`${apiBase}/auth/callback?${params.toString()}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'SSO callback failed');
      }
      const data = await res.json();
      persist(data.access_token, {
        id: data.user.id,
        email: data.user.email,
        name: data.user.name,
        shotgrid_user_id: data.user.shotgrid_user_id,
        auth_mode: 'shotgrid_sso',
      });
      window.history.replaceState({}, '', window.location.pathname);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'SSO login failed. Please try again.';
      console.error('[ShotGridAuth] SSO callback error:', msg);
      setSsoError(msg);
      clear();
    } finally {
      setIsLoading(false);
    }
  }, [apiBase, persist, clear]);

  // ── Popup relay: send auth params to parent then close ───────────────── //

  useEffect(() => {
    if (!isInPopup) return;

    const urlParams = new URLSearchParams(window.location.search);
    const sessionToken = urlParams.get('session_token');
    const code = urlParams.get('code');
    const state = urlParams.get('state');

    if (sessionToken || code) {
      window.opener.postMessage(
        { type: 'dna_sso_callback', sessionToken, code, state },
        window.location.origin,
      );
      window.close();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Listen for popup postMessage (parent window only) ────────────────── //

  useEffect(() => {
    if (isInPopup) return;

    const handlePopupMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== 'dna_sso_callback') return;

      const { sessionToken, code, state } = event.data as {
        sessionToken: string | null;
        code: string | null;
        state: string | null;
      };
      if (sessionToken || code) {
        handleSsoCallback(sessionToken, code, state);
      }
    };

    window.addEventListener('message', handlePopupMessage);
    return () => window.removeEventListener('message', handlePopupMessage);
  }, [isInPopup, handleSsoCallback]);

  // ── Fetch auth modes + validate stored token on mount ────────────────── //

  useEffect(() => {
    if (isInPopup) {
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    (async () => {
      // ── Step 1: Validate stored token ──────────────────────────────── //
      // Clears the token on ANY failure — network error OR non-2xx response.
      // This ensures a backend restart (which wipes Redis sessions) forces
      // the user back to the login page instead of letting them reach the
      // app with a dead session that returns 401 on every API call.
      const storedToken = sessionStorage.getItem(TOKEN_KEY);
      if (storedToken) {
        let tokenValid = false;
        try {
          const meRes = await fetch(`${apiBase}/auth/me`, {
            headers: { Authorization: `Bearer ${storedToken}` },
          });
          tokenValid = meRes.ok;
        } catch {
          // Network error (backend down / still starting) → treat as invalid
          tokenValid = false;
        }
        if (!tokenValid && !cancelled) {
          sessionStorage.removeItem(TOKEN_KEY);
          sessionStorage.removeItem(USER_KEY);
          setToken(null);
          setUser(null);
          apiHandler.setUser(null);
        }
      }

      // ── Step 2: Fetch available login modes ─────────────────────────── //
      try {
        const res = await fetch(`${apiBase}/auth/login`);
        if (!res.ok) throw new Error('Failed to fetch auth mode');
        const data = await res.json();
        if (cancelled) return;

        // Parse multi-mode response (new format) or fall back to legacy
        if (data.modes) {
          setLoginModes({
            shotgrid_pat: data.modes.shotgrid_pat ?? { enabled: true },
            shotgrid_sso: data.modes.shotgrid_sso ?? { enabled: false },
            google: data.modes.google ?? { enabled: false },
          });
        }
        // Legacy mode field for backward compat
        setMode((data.mode as ShotGridAuthMode) ?? 'pat');
        setModeWarning(data.warning ?? null);
      } catch (err) {
        console.error('[ShotGridAuth] Failed to detect auth mode:', err);
        if (!cancelled) setMode('none');
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── PAT sign-in ──────────────────────────────────────────────────────── //

  const signIn = useCallback(async (username: string, password: string) => {
    setIsLoading(true);
    try {
      const res = await fetch(`${apiBase}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Login failed');
      }
      const data = await res.json();
      persist(data.access_token, {
        id: data.user.id,
        email: data.user.email,
        name: data.user.name,
        shotgrid_user_id: data.user.shotgrid_user_id,
        auth_mode: 'shotgrid_pat',
      });
    } finally {
      setIsLoading(false);
    }
  }, [apiBase, persist]);

  // ── SSO popup launcher ───────────────────────────────────────────────── //

  const signInWithSso = useCallback((provider: 'shotgrid_sso' | 'google') => {
    setSsoError(null);

    // For ShotGrid SSO, fetch a fresh redirect URL then open a popup
    if (provider === 'shotgrid_sso') {
      fetch(`${apiBase}/auth/login`)
        .then((r) => r.json())
        .then((data) => {
          const url = data.modes?.shotgrid_sso?.redirect_url || data.redirect_url;
          if (!url) {
            setSsoError('No ShotGrid SSO URL returned. Check SHOTGRID_CLIENT_ID configuration.');
            return;
          }
          _openPopup(url);
        })
        .catch(() => setSsoError('Failed to initiate ShotGrid SSO. Please try again.'));
      return;
    }

    // Google SSO — handled externally via signInWithGoogleToken
    // (caller triggers useGoogleLogin then calls signInWithGoogleToken)
  }, [apiBase]);

  const _openPopup = (url: string) => {
    const width = 560, height = 680;
    const left = Math.round(window.screenX + (window.outerWidth - width) / 2);
    const top = Math.round(window.screenY + (window.outerHeight - height) / 2);
    const popup = window.open(url, 'dna_sso_login',
      `width=${width},height=${height},left=${left},top=${top},scrollbars=yes,resizable=yes`);
    if (!popup || popup.closed) {
      setSsoError('Popup was blocked. Please allow popups for this site, then try again.');
    } else {
      popup.focus();
    }
  };

  // ── Google token → DNA JWT ───────────────────────────────────────────── //

  const signInWithGoogleToken = useCallback(async (googleToken: string) => {
    setIsLoading(true);
    setSsoError(null);
    try {
      const res = await fetch(`${apiBase}/auth/google/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: googleToken }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Google login failed');
      }
      const data = await res.json();
      persist(data.access_token, {
        id: data.user.id,
        email: data.user.email,
        name: data.user.name,
        shotgrid_user_id: data.user.shotgrid_user_id,
        auth_mode: 'google',
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Google login failed.';
      setSsoError(msg);
      clear();
    } finally {
      setIsLoading(false);
    }
  }, [apiBase, persist, clear]);

  // ── Token refresh ────────────────────────────────────────────────────── //

  const refreshToken = useCallback(async () => {
    const currentToken = sessionStorage.getItem(TOKEN_KEY);
    if (!currentToken) return;
    // Google sessions don't have a SG refresh token — skip
    const storedUser = sessionStorage.getItem(USER_KEY);
    if (storedUser) {
      try {
        const u = JSON.parse(storedUser) as ShotGridUser;
        if (u.auth_mode === 'google') return;
      } catch { /* ignore */ }
    }
    try {
      const res = await fetch(`${apiBase}/auth/refresh`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${currentToken}` },
      });
      if (!res.ok) { clear(); return; }
      const data = await res.json();
      const currentUser = sessionStorage.getItem(USER_KEY);
      const parsedUser: ShotGridUser | null = currentUser ? JSON.parse(currentUser) : null;
      if (parsedUser) {
        persist(data.access_token, { ...parsedUser, ...data.user });
      }
    } catch (err) {
      console.error('[ShotGridAuth] Token refresh failed:', err);
      clear();
    }
  }, [apiBase, persist, clear]);

  // Auto-refresh every 25 minutes
  useEffect(() => {
    if (!token) return;
    refreshTimerRef.current = setInterval(refreshToken, REFRESH_INTERVAL_MS);
    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [token, refreshToken]);

  // Restore apiHandler on mount if token already in sessionStorage
  useEffect(() => {
    if (token && user) {
      apiHandler.setUser({ id: String(user.id), email: user.email, name: user.name, token });
    }
  // Run only on mount
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Sign-out ─────────────────────────────────────────────────────────── //

  const signOut = useCallback(async () => {
    const currentToken = sessionStorage.getItem(TOKEN_KEY);
    if (currentToken) {
      try {
        await fetch(`${apiBase}/auth/logout`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${currentToken}` },
        });
      } catch { /* best-effort */ }
    }
    clear();
  }, [apiBase, clear]);

  const value: ShotGridAuthContextValue = {
    isAuthenticated: !!token && !!user,
    isLoading,
    loginModes,
    mode,
    modeWarning,
    ssoError,
    clearSsoError,
    user,
    token,
    authProvider: 'shotgrid',
    signIn,
    signInWithSso,
    signInWithGoogleToken,
    signOut,
    refreshToken,
  };

  return (
    <ShotGridAuthContext.Provider value={value}>
      {children}
    </ShotGridAuthContext.Provider>
  );
}

export function useShotGridAuth(): ShotGridAuthContextValue {
  const ctx = useContext(ShotGridAuthContext);
  if (!ctx) throw new Error('useShotGridAuth must be used within ShotGridAuthProvider');
  return ctx;
}
