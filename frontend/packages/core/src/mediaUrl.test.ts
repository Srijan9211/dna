import { describe, expect, it } from 'vitest';
import { resolveMediaUrl } from './utils';

describe('resolveMediaUrl', () => {
  it('prefixes a relative proxy URL with the API base, with or without a trailing slash', () => {
    expect(
      resolveMediaUrl('/media/versions/1/thumbnail?token=T', 'https://api')
    ).toBe('https://api/media/versions/1/thumbnail?token=T');
    expect(
      resolveMediaUrl('/media/versions/1/thumbnail?token=T', 'https://api//')
    ).toBe('https://api/media/versions/1/thumbnail?token=T');
  });

  it('keeps an AYON id (with its colon) intact in the path', () => {
    expect(
      resolveMediaUrl('/media/versions/demo:abc/movie?token=T', 'http://x')
    ).toBe('http://x/media/versions/demo:abc/movie?token=T');
  });

  it('returns absolute and other URLs unchanged', () => {
    for (const url of [
      'https://cdn.example/a.jpg',
      'data:image/png;base64,AA',
      '/static/logo.png',
    ]) {
      expect(resolveMediaUrl(url, 'https://api')).toBe(url);
    }
  });

  it('returns undefined when there is no media', () => {
    for (const none of [undefined, null, '']) {
      expect(resolveMediaUrl(none, 'https://api')).toBeUndefined();
    }
  });

  it('with an empty base the URL stays relative to the page (same-origin deployments)', () => {
    expect(resolveMediaUrl('/media/versions/1/movie?token=T', '')).toBe(
      '/media/versions/1/movie?token=T'
    );
  });
});
