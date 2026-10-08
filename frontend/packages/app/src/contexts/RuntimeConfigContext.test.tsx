import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import {
  RuntimeConfigProvider,
  useRuntimeConfig,
} from './RuntimeConfigContext';

function Probe() {
  const c = useRuntimeConfig();
  return (
    <div>
      <span data-testid="auth">{c.authProvider}</span>
      <span data-testid="source">{c.source}</span>
      <span data-testid="label">{c.tracker.labels.playlist}</span>
    </div>
  );
}

const respond = (body: unknown) =>
  vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(body) });

describe('RuntimeConfigProvider', () => {
  it('renders nothing but a loading state until the server answers', async () => {
    let release!: (value: unknown) => void;
    const fetchImpl = vi.fn(
      () =>
        new Promise((resolve) => {
          release = resolve;
        })
    );
    render(
      <RuntimeConfigProvider
        env={{}}
        fetchOptions={{ fetchImpl: fetchImpl as unknown as typeof fetch }}
      >
        <Probe />
      </RuntimeConfigProvider>
    );
    expect(screen.getByRole('status')).toHaveTextContent('Loading');
    expect(screen.queryByTestId('auth')).toBeNull(); // no component starts with the wrong login mode

    release({
      ok: true,
      json: () => Promise.resolve({ auth: { provider: 'ayon' } }),
    });
    await waitFor(() =>
      expect(screen.getByTestId('auth')).toHaveTextContent('ayon')
    );
    expect(screen.getByTestId('source')).toHaveTextContent('server');
  });

  it('provides the tracker vocabulary to the tree', async () => {
    render(
      <RuntimeConfigProvider
        env={{}}
        fetchOptions={{
          fetchImpl: respond({
            prodtrack: { labels: { playlist: 'List' } },
          }) as unknown as typeof fetch,
        }}
      >
        <Probe />
      </RuntimeConfigProvider>
    );
    await waitFor(() =>
      expect(screen.getByTestId('label')).toHaveTextContent('List')
    );
  });

  it('falls back to the build-time environment when the server cannot be read', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('network'));
    render(
      <RuntimeConfigProvider
        env={{ VITE_AUTH_PROVIDER: 'none' }}
        fetchOptions={{ fetchImpl: fetchImpl as unknown as typeof fetch }}
      >
        <Probe />
      </RuntimeConfigProvider>
    );
    await waitFor(() =>
      expect(screen.getByTestId('auth')).toHaveTextContent('none')
    );
    expect(screen.getByTestId('source')).toHaveTextContent('build');
  });

  it('can skip the network entirely', () => {
    const fetchImpl = vi.fn();
    render(
      <RuntimeConfigProvider
        env={{ VITE_AUTH_PROVIDER: 'shotgrid' }}
        skipFetch
        fetchOptions={{ fetchImpl }}
      >
        <Probe />
      </RuntimeConfigProvider>
    );
    expect(screen.getByTestId('auth')).toHaveTextContent('shotgrid'); // synchronous
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('useRuntimeConfig outside a provider', () => {
  it('falls back to the build-time environment, so components render in isolation', () => {
    render(<Probe />);
    expect(screen.getByTestId('source')).toHaveTextContent('build');
    expect(screen.getByTestId('label')).toHaveTextContent('Playlist');
  });
});
