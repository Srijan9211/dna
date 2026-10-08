import { describe, expect, it } from 'vitest';
import type { HandshakeReport } from '@dna/core';
import {
  CONNECTORS,
  applicableSuggestions,
  buildCandidate,
  canSave,
  connectorSpec,
  isAdminRoute,
  isSecretReference,
  mergeOverrides,
  summarizeChecks,
  validateForm,
} from './trackerSetupLogic';

const report = (over: Partial<HandshakeReport> = {}): HandshakeReport => ({
  connector: 'ayon',
  contract: 'dna.prodtrack/1',
  ok: true,
  checks: [{ name: 'connect', status: 'ok', message: 'ok', details: {} }],
  suggestions: [],
  capabilities: {},
  schema: {},
  applied: false,
  ...over,
});

describe('secret references', () => {
  it.each([
    '${env:AYON_API_KEY}',
    '${env:KEY:-fallback}',
    '${file:/run/secrets/ayon_key}',
    '${file:C:/secrets/key}',
    '  ${env:PADDED}  ',
  ])('accepts %s', (value) => expect(isSecretReference(value)).toBe(true));

  it.each([
    'abcd1234realkey',
    '',
    '${env:}',
    '${env:has space}',
    'prefix ${env:X}',
    '${other:X}',
    '$env:X',
  ])('rejects %j', (value) => expect(isSecretReference(value)).toBe(false));
});

describe('validateForm', () => {
  it('requires the connection fields', () => {
    expect(Object.keys(validateForm('ayon', {})).sort()).toEqual([
      'api_key',
      'url',
    ]);
    expect(Object.keys(validateForm('shotgrid', {})).sort()).toEqual([
      'api_key',
      'script_name',
      'url',
    ]);
  });

  it('refuses a pasted secret and says what to enter instead', () => {
    const errors = validateForm('ayon', {
      url: 'http://a',
      api_key: 'plain-key-value',
    });
    expect(errors.api_key).toMatch(/reference/);
    expect(errors.api_key).toMatch(/never stored as plain text/);
  });

  it('accepts a complete form with a reference', () => {
    expect(
      validateForm('ayon', { url: 'http://a', api_key: '${env:AYON_API_KEY}' })
    ).toEqual({});
  });

  it('needs nothing for the mock connector', () => {
    expect(validateForm('mock', {})).toEqual({});
  });
});

describe('buildCandidate', () => {
  it('builds the connector and its connection settings, trimmed', () => {
    expect(
      buildCandidate('ayon', {
        url: ' http://a ',
        api_key: '${env:K}',
        sudo_user: '',
      })
    ).toEqual({
      prodtrack: {
        connector: 'ayon',
        ayon: { url: 'http://a', api_key: '${env:K}' },
      },
    });
  });

  it('leaves empty settings out so defaults and the environment still apply', () => {
    expect(buildCandidate('shotgrid', {})).toEqual({
      prodtrack: { connector: 'shotgrid' },
    });
  });

  it('carries accepted profile overrides', () => {
    const overrides = {
      entities: { shot: { where: { folderType: ['Shot'] } } },
    };
    expect(buildCandidate('ayon', { url: 'http://a' }, overrides)).toEqual({
      prodtrack: {
        connector: 'ayon',
        ayon: { url: 'http://a' },
        profile: overrides,
      },
    });
  });

  it('never invents a connection block for the mock connector', () => {
    expect(buildCandidate('mock', { url: 'ignored' })).toEqual({
      prodtrack: { connector: 'mock' },
    });
  });
});

describe('mergeOverrides (mirrors the backend merge)', () => {
  it('merges objects key by key', () => {
    expect(
      mergeOverrides(
        {
          entities: { version: { fields: { a: 'x' } } },
          labels: { shot: 'Plate' },
        },
        { entities: { version: { fields: { b: 'y' } } } }
      )
    ).toEqual({
      entities: { version: { fields: { a: 'x', b: 'y' } } },
      labels: { shot: 'Plate' },
    });
  });

  it('replaces a list whole, and keeps null (which means "remove this key")', () => {
    expect(
      mergeOverrides(
        {
          entities: { shot: { where: { folderType: ['Shotz', 'Sequence'] } } },
        },
        { entities: { shot: { where: { folderType: ['Shot', 'Sequence'] } } } }
      )
    ).toEqual({
      entities: { shot: { where: { folderType: ['Shot', 'Sequence'] } } },
    });
    expect(
      mergeOverrides({}, { fields: { old: null, new: 'status' } })
    ).toEqual({
      fields: { old: null, new: 'status' },
    });
  });

  it('does not mutate its inputs', () => {
    const base = { a: { b: 1 } };
    const patch = { a: { c: 2 } };
    mergeOverrides(base, patch);
    expect(base).toEqual({ a: { b: 1 } });
    expect(patch).toEqual({ a: { c: 2 } });
  });

  it('accepting two suggestions in a row accumulates both fixes', () => {
    const first = {
      entities: {
        version: { fields: { sg_status_list: null, sg_status: 'status' } },
      },
    };
    const second = { entities: { shot: { where: { folderType: ['Shot'] } } } };
    expect(mergeOverrides(mergeOverrides({}, first), second)).toEqual({
      entities: { ...first.entities, ...second.entities },
    });
  });
});

describe('report helpers', () => {
  it('summarises checks by status', () => {
    const r = report({
      checks: [
        { name: 'a', status: 'ok', message: '', details: {} },
        { name: 'b', status: 'ok', message: '', details: {} },
        { name: 'c', status: 'warn', message: '', details: {} },
        { name: 'd', status: 'fail', message: '', details: {} },
        { name: 'e', status: 'skip', message: '', details: {} },
      ],
    });
    expect(summarizeChecks(r)).toEqual({ ok: 2, warn: 1, fail: 1, skip: 1 });
  });

  it('only a passing report can be saved, and warnings do not block it', () => {
    expect(canSave(null)).toBe(false);
    expect(canSave(report())).toBe(true);
    expect(canSave(report({ ok: false }))).toBe(false);
    expect(canSave(report({ checks: [] }))).toBe(false);
    expect(
      canSave(
        report({
          checks: [{ name: 'n', status: 'warn', message: '', details: {} }],
        })
      )
    ).toBe(true);
  });

  it('offers only suggestions that carry a ready override', () => {
    const r = report({
      suggestions: [
        { path: 'a', problem: 'p', options: ['x'], override: { a: 1 } },
        { path: 'b', problem: 'q', options: ['y'], override: null },
      ],
    });
    expect(applicableSuggestions(r).map((s) => s.path)).toEqual(['a']);
    expect(applicableSuggestions(null)).toEqual([]);
  });
});

describe('connectors and routing', () => {
  it('lists AYON, ShotGrid and the mock, each with a spec', () => {
    expect(CONNECTORS.map((c) => c.id)).toEqual(['ayon', 'shotgrid', 'mock']);
    expect(
      connectorSpec('ayon').fields.find((f) => f.key === 'api_key')?.secret
    ).toBe(true);
    expect(() => connectorSpec('nope' as never)).toThrow(/Unknown connector/);
  });

  it('marks every secret field in every connector (nothing secret is a plain field)', () => {
    for (const c of CONNECTORS) {
      for (const f of c.fields) {
        if (/key|secret|password|token/i.test(f.key))
          expect(f.secret).toBe(true);
      }
    }
  });

  it('recognises the admin route', () => {
    expect(isAdminRoute('?admin=config')).toBe(true);
    expect(isAdminRoute('?project=1&admin=config')).toBe(true);
    expect(isAdminRoute('?admin=other')).toBe(false);
    expect(isAdminRoute('')).toBe(false);
  });
});
