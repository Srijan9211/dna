import { describe, expect, it } from 'vitest';
import { coerceEntityId, parseEntityId } from './utils';
import { mentionResultKey, parseMentionKey } from './mentionLookup';

// The same ids the backend test-suite uses (backend/tests/test_entity_ids.py), so
// the two sides agree on what an id is.
const AYON_ID = 'dna_test:5fd4ea6ac19911f1877e42dd13c3d9cf';

describe('parseEntityId', () => {
  it('reads a short run of digits as a number (ShotGrid ids)', () => {
    expect(parseEntityId('42')).toBe(42);
    expect(parseEntityId(' 42 ')).toBe(42);
    expect(parseEntityId('123456789012345')).toBe(123456789012345);
  });

  it('keeps anything else as the string it is (AYON ids)', () => {
    expect(parseEntityId(AYON_ID)).toBe(AYON_ID);
    expect(parseEntityId('srijan9211')).toBe('srijan9211');
  });

  it('never turns a long all-digit id into a (lossy) number', () => {
    // 16+ digits cannot be represented exactly as a JS number
    expect(parseEntityId('1234567890123456')).toBe('1234567890123456');
    expect(parseEntityId('12345678901234567890123456789012')).toBe(
      '12345678901234567890123456789012'
    );
  });

  it('returns null for empty or non-positive input', () => {
    expect(parseEntityId(null)).toBeNull();
    expect(parseEntityId(undefined)).toBeNull();
    expect(parseEntityId('')).toBeNull();
    expect(parseEntityId('   ')).toBeNull();
    expect(parseEntityId('0')).toBeNull();
  });
});

describe('coerceEntityId', () => {
  it('accepts positive integers and id-like text', () => {
    expect(coerceEntityId(7)).toBe(7);
    expect(coerceEntityId('7')).toBe(7);
    expect(coerceEntityId(AYON_ID)).toBe(AYON_ID);
  });

  it('rejects things that cannot be an id', () => {
    for (const bad of [0, -1, 1.5, NaN, null, undefined, true, {}, [], '']) {
      expect(coerceEntityId(bad)).toBeNull();
    }
  });
});

describe('parseMentionKey', () => {
  it('splits a ShotGrid mention key', () => {
    expect(parseMentionKey('shot:42')).toEqual({ type: 'shot', id: 42 });
  });

  it('splits on the FIRST colon only, so an AYON id keeps its own colon', () => {
    expect(parseMentionKey(`shot:${AYON_ID}`)).toEqual({
      type: 'shot',
      id: AYON_ID,
    });
  });

  it('round-trips with mentionResultKey for both id shapes', () => {
    for (const id of [42, AYON_ID, 'srijan9211']) {
      const key = mentionResultKey({ type: 'Version', id, name: 'n' });
      expect(parseMentionKey(key)).toEqual({ type: 'version', id });
    }
  });

  it('returns null for a malformed key', () => {
    for (const bad of ['', 'nocolon', ':42', 'shot:', 'shot:0']) {
      expect(parseMentionKey(bad)).toBeNull();
    }
  });
});
