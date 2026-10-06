import { describe, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { RuntimeConfigProvider } from '../contexts/RuntimeConfigContext';
import { useLabels } from './useLabels';

function Probe() {
  const label = useLabels();
  return (
    <ul>
      <li data-testid="playlist">{label('playlist')}</li>
      <li data-testid="shot">{label('Shot')}</li>
      <li data-testid="unknown">{label('Sequence')}</li>
    </ul>
  );
}

const serverWith = (labels: Record<string, string>) =>
  (() =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ prodtrack: { labels } }),
    })) as unknown as typeof fetch;

describe('useLabels', () => {
  it("uses the tracker's own words, so AYON users see 'List' not 'Playlist'", async () => {
    render(
      <RuntimeConfigProvider env={{}} fetchOptions={{ fetchImpl: serverWith({ playlist: 'List' }) }}>
        <Probe />
      </RuntimeConfigProvider>
    );
    await waitFor(() => expect(screen.getByTestId('playlist')).toHaveTextContent('List'));
    expect(screen.getByTestId('shot')).toHaveTextContent('Shot'); // untouched labels keep the default
    expect(screen.getByTestId('unknown')).toHaveTextContent('Sequence'); // unknown kinds echo back
  });

  it('keeps the familiar ShotGrid words with no server config', () => {
    render(
      <RuntimeConfigProvider env={{}} skipFetch>
        <Probe />
      </RuntimeConfigProvider>
    );
    expect(screen.getByTestId('playlist')).toHaveTextContent('Playlist');
  });
});
