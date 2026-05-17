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

export interface ShotGridUser {
  id: number | string;
  email: string;
  name: string;
  shotgrid_user_id?: number;
  auth_mode?: ShotGridAuthMode;
}

interface ShotGridAuthContextValue {
  isAuthenticated: boolean;
  isLoading: boolean;
  mode: ShotGridAuthMode;
  user: ShotGridUser | null;
  token: string | null;
  authProvider: 'shotgrid';
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshToken: () => Promise<void>;
}

const ShotGridAuthContext = createContext<ShotGridAuthContextValue | null>(null);

interface ShotGridAuthProviderProps {
  children: ReactNode;
}

export function ShotGridAuthProvider({ children }: ShotGridAuthProviderProps) {
  const [mode, setMode] = useState<ShotGridAuthMode>(null);
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

  // ── Fetch auth mode + validate any stored token on mount ────────────── //

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // 1. Validate stored token first — clears stale/expired sessions before
        //    rendering any data-fetching components.
        const storedToken = sessionStorage.getItem(TOKEN_KEY);
        if (storedToken) {
          const meRes = await fetch(`${apiBase}/auth/me`, {
            headers: { Authorization: `Bearer ${storedToken}` },
          });
          if (!meRes.ok) {
            // Token is expired or revoked — clear the session silently
            sessionStorage.removeItem(TOKEN_KEY);
            sessionStorage.removeItem(USER_KEY);
            setToken(null);
            setUser(null);
            apiHandler.setUser(null);
          }
        }

        // 2. Detect auth mode from backend
        const res = await fetch(`${apiBase}/auth/login`);
        if (!res.ok) throw new Error('Failed to fetch auth mode');
        const data: { mode: ShotGridAuthMode; redirect_url?: string } = await res.json();
        if (cancelled) return;
        setMode(data.mode);

        // SSO mode: if we have ?session_token in the URL, complete the callback
        if (data.mode === 'sso') {
          const params = new URLSearchParams(window.location.search);
          const sessionToken = params.get('session_token');
          const state = params.get('state');
          if (sessionToken) {
            await handleSsoCallback(sessionToken, state);
          }
        }
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

  // ── SSO callback handler ─────────────────────────────────────────────── //

  const handleSsoCallback = useCallback(async (sessionToken: string, state: string | null) => {
    setIsLoading(true);
    try {
      const params = new URLSearchParams();
      if (state) params.set('state', state);
      const url = `${apiBase}/auth/callback?session_token=${encodeURIComponent(sessionToken)}${state ? `&state=${encodeURIComponent(state)}` : ''}`;
      const res = await fetch(url);
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
        auth_mode: 'sso',
      });
      // Clean up the URL query params
      window.history.replaceState({}, '', window.location.pathname);
    } catch (err) {
      console.error('[ShotGridAuth] SSO callback error:', err);
      clear();
    } finally {
      setIsLoading(false);
    }
  }, [apiBase, persist, clear]);

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
        auth_mode: 'pat',
      });
    } finally {
      setIsLoading(false);
    }
  }, [apiBase, persist]);

  // ── Token refresh ────────────────────────────────────────────────────── //

  const refreshToken = useCallback(async () => {
    const currentToken = sessionStorage.getItem(TOKEN_KEY);
    if (!currentToken) return;
    try {
      const res = await fetch(`${apiBase}/auth/refresh`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${currentToken}` },
      });
      if (!res.ok) {
        clear();
        return;
      }
      const data = await res.json();
      const currentUser = sessionStorage.getItem(USER_KEY);
      const parsedUser: ShotGridUser | null = currentUser
        ? JSON.parse(currentUser)
        : null;
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
    mode,
    user,
    token,
    authProvider: 'shotgrid',
    signIn,
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
