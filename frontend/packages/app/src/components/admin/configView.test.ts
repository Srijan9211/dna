import { describe, expect, it } from 'vitest';
import { describeSource, flattenConfig } from './configView';

describe('flattenConfig', () => {
  it('produces dotted paths that match the provenance keys', () => {
    expect(
      flattenConfig('prodtrack', {
        connector: 'ayon',
        ayon: { url: 'http://a', timeout: 30 },
      })
    ).toEqual([
      { path: 'prodtrack.connector', value: 'ayon' },
      { path: 'prodtrack.ayon.url', value: 'http://a' },
      { path: 'prodtrack.ayon.timeout', value: '30' },
    ]);
  });

  it('shows null, empty, list and boolean values readably', () => {
    expect(
      flattenConfig('s', { a: null, b: '', c: ['x', 'y'], d: false, e: {} })
    ).toEqual([
      { path: 's.a', value: '—' },
      { path: 's.b', value: '""' },
      { path: 's.c', value: '["x","y"]' },
      { path: 's.d', value: 'false' },
      { path: 's.e', value: '{}' },
    ]);
  });

  it('shows a masked secret as the mask, never anything else', () => {
    expect(flattenConfig('p', { api_key: '********' })).toEqual([
      { path: 'p.api_key', value: '********' },
    ]);
  });
});

describe('describeSource', () => {
  it.each([
    [undefined, 'default'],
    ['default', 'default'],
    ['runtime', 'saved in the admin page'],
    ['env:AYON_URL', 'environment AYON_URL'],
    ['file:/etc/dna/site.yaml', 'file /etc/dna/site.yaml'],
    ['something', 'something'],
  ])('%s -> %s', (input, expected) =>
    expect(describeSource(input)).toBe(expected)
  );
});
