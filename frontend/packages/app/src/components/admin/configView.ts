/** Helpers for showing the effective configuration as rows. */

export interface ConfigRow {
  path: string;
  value: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function show(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value === '' ? '""' : value;
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/**
 * Flatten one section into `path -> value` rows, in declaration order.  `path`
 * includes the section name so it matches the provenance keys (`prodtrack.ayon.url`).
 * An empty mapping is shown as a row of its own, so it is not silently dropped.
 */
export function flattenConfig(section: string, values: unknown): ConfigRow[] {
  const rows: ConfigRow[] = [];
  const walk = (node: unknown, prefix: string) => {
    if (isRecord(node) && Object.keys(node).length > 0) {
      for (const [key, child] of Object.entries(node))
        walk(child, `${prefix}.${key}`);
    } else {
      rows.push({ path: prefix, value: show(node) });
    }
  };
  walk(values, section);
  return rows;
}

/** A short, human label for a provenance string (`env:AYON_URL` -> `environment AYON_URL`). */
export function describeSource(source: string | undefined): string {
  if (!source || source === 'default') return 'default';
  if (source === 'runtime') return 'saved in the admin page';
  if (source.startsWith('env:')) return `environment ${source.slice(4)}`;
  if (source.startsWith('file:')) return `file ${source.slice(5)}`;
  return source;
}
