/** Pure helpers for the per-project overrides screen. */
import type { ProjectOverride, ProjectSetting } from '@dna/core';

export type ParsedValue =
  | { ok: true; value: unknown }
  | { ok: false; error: string };

/** Turn what was typed into the value the setting's type needs. */
export function parseSettingValue(
  type: ProjectSetting['type'],
  raw: string
): ParsedValue {
  const text = raw.trim();
  switch (type) {
    case 'boolean':
      if (text === 'true') return { ok: true, value: true };
      if (text === 'false') return { ok: true, value: false };
      return { ok: false, error: 'Choose on or off.' };
    case 'integer':
      if (/^-?\d+$/.test(text)) return { ok: true, value: Number(text) };
      return { ok: false, error: 'Enter a whole number.' };
    case 'number': {
      const n = Number(text);
      if (text !== '' && Number.isFinite(n)) return { ok: true, value: n };
      return { ok: false, error: 'Enter a number.' };
    }
    default:
      if (text === '') return { ok: false, error: 'Enter a value.' };
      return { ok: true, value: text };
  }
}

/** `{section: {a: {b: value}}}` for a setting at `a.b` in `section`. */
export function buildProjectPatch(
  section: string,
  path: string,
  value: unknown
): Record<string, unknown> {
  const keys = path.split('.');
  let node: unknown = value;
  for (let i = keys.length - 1; i >= 0; i -= 1) node = { [keys[i]]: node };
  return { [section]: node };
}

/** A stable key for a setting: `section.path`. */
export function settingKey(s: { section: string; path: string }): string {
  return `${s.section}.${s.path}`;
}

/** How a value is shown in a table cell. */
export function showValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  if (typeof value === 'string') return value === '' ? '""' : value;
  return JSON.stringify(value);
}

/** Overrides grouped by project, projects in name order. */
export function groupByProject(
  overrides: ProjectOverride[]
): Array<{ project: string; items: ProjectOverride[] }> {
  const groups = new Map<string, ProjectOverride[]>();
  for (const o of overrides) {
    const list = groups.get(o.project) ?? [];
    list.push(o);
    groups.set(o.project, list);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([project, items]) => ({ project, items }));
}

/** An override can be removed here only if it was set here, not in a file. */
export function isRemovable(o: ProjectOverride): boolean {
  return o.source === 'runtime';
}

/** A project id as the server accepts it. */
export function isValidProjectId(id: string): boolean {
  return /^[A-Za-z0-9_.:-]{1,128}$/.test(id);
}
