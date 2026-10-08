import { describe, expect, it } from 'vitest';
import type { ProjectOverride } from '@dna/core';
import {
  buildProjectPatch,
  groupByProject,
  isRemovable,
  isValidProjectId,
  parseSettingValue,
  settingKey,
  showValue,
} from './projectOverrides';

describe('parseSettingValue', () => {
  it('reads booleans only as on or off', () => {
    expect(parseSettingValue('boolean', 'true')).toEqual({
      ok: true,
      value: true,
    });
    expect(parseSettingValue('boolean', 'false')).toEqual({
      ok: true,
      value: false,
    });
    expect(parseSettingValue('boolean', 'yes').ok).toBe(false);
    expect(parseSettingValue('boolean', '').ok).toBe(false);
  });

  it('reads integers strictly', () => {
    expect(parseSettingValue('integer', ' 42 ')).toEqual({
      ok: true,
      value: 42,
    });
    expect(parseSettingValue('integer', '-3')).toEqual({
      ok: true,
      value: -3,
    });
    for (const bad of ['4.5', 'abc', '', '1e3', '0x10'])
      expect(parseSettingValue('integer', bad).ok).toBe(false);
  });

  it('reads numbers but not blanks or non-numbers', () => {
    expect(parseSettingValue('number', '2.5')).toEqual({
      ok: true,
      value: 2.5,
    });
    for (const bad of ['', '  ', 'NaN', 'Infinity', 'x'])
      expect(parseSettingValue('number', bad).ok).toBe(false);
  });

  it('keeps strings as typed but refuses an empty one', () => {
    expect(parseSettingValue('string', ' CustomEntity05 ')).toEqual({
      ok: true,
      value: 'CustomEntity05',
    });
    expect(parseSettingValue('string', '   ').ok).toBe(false);
  });
});

describe('buildProjectPatch', () => {
  it('nests a dotted path inside its section', () => {
    expect(
      buildProjectPatch('prodtrack', 'shotgrid.transcript_entity', 'X')
    ).toEqual({ prodtrack: { shotgrid: { transcript_entity: 'X' } } });
    expect(buildProjectPatch('features', 'transcript_publish', true)).toEqual({
      features: { transcript_publish: true },
    });
  });
});

describe('display helpers', () => {
  it('shows values the way people read them', () => {
    expect(showValue(true)).toBe('on');
    expect(showValue(false)).toBe('off');
    expect(showValue('')).toBe('""');
    expect(showValue(null)).toBe('—');
    expect(showValue(5)).toBe('5');
    expect(showValue({ a: 1 })).toBe('{"a":1}');
  });

  it('keys a setting by section and path', () => {
    expect(
      settingKey({ section: 'features', path: 'transcript_publish' })
    ).toBe('features.transcript_publish');
  });

  it('groups overrides by project in name order', () => {
    const make = (project: string, path: string): ProjectOverride => ({
      project,
      section: 'features',
      path,
      value: true,
      source: 'runtime',
    });
    const groups = groupByProject([
      make('b', 'x'),
      make('a', 'y'),
      make('b', 'z'),
    ]);
    expect(groups.map((g) => g.project)).toEqual(['a', 'b']);
    expect(groups[1].items.map((i) => i.path)).toEqual(['x', 'z']);
  });

  it('allows removing only what was set here', () => {
    const base = { project: 'p', section: 's', path: 'a', value: 1 };
    expect(isRemovable({ ...base, source: 'runtime' })).toBe(true);
    expect(isRemovable({ ...base, source: 'file:/etc/dna/site.yaml' })).toBe(
      false
    );
  });

  it('accepts the project ids the server accepts', () => {
    for (const ok of ['dna_test', '123', 'a.b-c:d', 'A'.repeat(128)])
      expect(isValidProjectId(ok)).toBe(true);
    for (const bad of ['', 'a b', 'a/b', 'a;b', 'x'.repeat(129)])
      expect(isValidProjectId(bad)).toBe(false);
  });
});
