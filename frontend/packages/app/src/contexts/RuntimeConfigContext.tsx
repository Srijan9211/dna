/* eslint-disable react-refresh/only-export-components */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import {
  fetchPublicConfig,
  resolveConfig,
  type BuildEnv,
  type FetchOptions,
  type ResolvedConfig,
} from '../config/runtimeConfig';

const RuntimeConfigContext = createContext<ResolvedConfig | null>(null);

interface RuntimeConfigProviderProps {
  children: ReactNode;
  /** Build-time environment; defaults to `import.meta.env`. */
  env?: BuildEnv;
  /** Skip the network read and use build-time values only (tests, storybook). */
  skipFetch?: boolean;
  fetchOptions?: FetchOptions;
}

/**
 * Loads the server's public config once at boot and provides the resolved result.
 * Nothing renders until it settles (a few hundred ms), so no component ever starts
 * with one login mode and then switches to another.
 */
export function RuntimeConfigProvider({
  children,
  env = import.meta.env as BuildEnv,
  skipFetch = false,
  fetchOptions,
}: RuntimeConfigProviderProps) {
  const [config, setConfig] = useState<ResolvedConfig | null>(() =>
    skipFetch ? resolveConfig(null, env) : null
  );

  useEffect(() => {
    if (skipFetch) return;
    let cancelled = false;
    fetchPublicConfig(env.VITE_API_BASE_URL ?? '', fetchOptions).then(
      (server) => {
        if (!cancelled) setConfig(resolveConfig(server, env));
      }
    );
    return () => {
      cancelled = true;
    };
    // The environment and options are fixed for the life of the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (config === null) {
    return (
      <div
        role="status"
        aria-live="polite"
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100vh',
          fontFamily: 'system-ui, sans-serif',
          color: '#667085',
        }}
      >
        Loading…
      </div>
    );
  }
  return (
    <RuntimeConfigContext.Provider value={config}>
      {children}
    </RuntimeConfigContext.Provider>
  );
}

/**
 * The resolved configuration.  Outside a provider (a unit test rendering one
 * component) it falls back to the build-time environment, which is what the app
 * did before runtime config existed.
 */
export function useRuntimeConfig(): ResolvedConfig {
  const fromProvider = useContext(RuntimeConfigContext);
  const fallback = useMemo(
    () => resolveConfig(null, import.meta.env as BuildEnv),
    []
  );
  return fromProvider ?? fallback;
}
