/**
 * Pure logic behind the tracker-setup wizard: what each connector needs, how the form
 * becomes a candidate configuration, and how an accepted suggestion edits it.  Kept
 * out of the components so it can be tested without rendering anything.
 */
import type { HandshakeReport, HandshakeSuggestion } from '@dna/core';

export type ConnectorId = 'shotgrid' | 'ayon' | 'mock';

export interface ConnectorField {
  key: string;
  label: string;
  placeholder?: string;
  help?: string;
  /** A secret: only a reference (`${env:NAME}`) may be entered, never the value. */
  secret?: boolean;
  required?: boolean;
  options?: string[];
}

export interface ConnectorSpec {
  id: ConnectorId;
  label: string;
  summary: string;
  fields: ConnectorField[];
}

export const CONNECTORS: ConnectorSpec[] = [
  {
    id: 'ayon',
    label: 'AYON',
    summary:
      'Folders as shots and assets, entity lists as playlists, comments as notes.',
    fields: [
      {
        key: 'url',
        label: 'Server URL',
        placeholder: 'http://ayon.example:5000',
        required: true,
      },
      {
        key: 'api_key',
        label: 'Service-user API key',
        placeholder: '${env:AYON_API_KEY}',
        help: 'A service user that may act as other users (x-as-user). Enter a reference to where the key is stored, not the key itself.',
        secret: true,
        required: true,
      },
      {
        key: 'sudo_user',
        label: 'Default user (optional)',
        help: 'Acted as for background jobs. Leave empty to use the service account.',
      },
    ],
  },
  {
    id: 'shotgrid',
    label: 'ShotGrid',
    summary: 'Shots, assets, versions and playlists in a ShotGrid site.',
    fields: [
      {
        key: 'url',
        label: 'Site URL',
        placeholder: 'https://studio.shotgrid.autodesk.com',
        required: true,
      },
      { key: 'script_name', label: 'Script name', required: true },
      {
        key: 'api_key',
        label: 'Script API key',
        placeholder: '${env:SHOTGRID_API_KEY}',
        secret: true,
        required: true,
        help: 'Enter a reference to where the key is stored, not the key itself.',
      },
      {
        key: 'site_type',
        label: 'Site type',
        options: ['cloud', 'onprem'],
      },
    ],
  },
  {
    id: 'mock',
    label: 'Mock (sample data)',
    summary: 'Local sample data for trying DNA out. Needs no connection.',
    fields: [],
  },
];

export function connectorSpec(id: ConnectorId): ConnectorSpec {
  const spec = CONNECTORS.find((c) => c.id === id);
  if (!spec) throw new Error(`Unknown connector '${id}'`);
  return spec;
}

/** `${env:NAME}`, `${env:NAME:-default}` or `${file:/path}` as a whole string. */
const REFERENCE = /^\$\{(env:[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?|file:[^}]+)\}$/;

export function isSecretReference(value: string): boolean {
  return REFERENCE.test(value.trim());
}

export type FormValues = Record<string, string>;

/** Problems with the connection form, by field key. Empty when it is usable. */
export function validateForm(
  connector: ConnectorId,
  values: FormValues
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of connectorSpec(connector).fields) {
    const value = (values[field.key] ?? '').trim();
    if (field.required && !value) {
      errors[field.key] = `${field.label} is required.`;
    } else if (field.secret && value && !isSecretReference(value)) {
      errors[field.key] =
        'Enter a reference such as ${env:NAME} or ${file:/path}. Secrets are never stored as plain text.';
    }
  }
  return errors;
}

export type ProfileOverrides = Record<string, unknown>;

/**
 * The candidate configuration to hand to the handshake: the chosen connector, its
 * connection settings (empty ones left out so defaults and environment still apply),
 * and any profile overrides accepted so far.
 */
export function buildCandidate(
  connector: ConnectorId,
  values: FormValues,
  profileOverrides: ProfileOverrides = {}
): Record<string, unknown> {
  const connection: Record<string, string> = {};
  for (const field of connectorSpec(connector).fields) {
    const value = (values[field.key] ?? '').trim();
    if (value) connection[field.key] = value;
  }
  const prodtrack: Record<string, unknown> = { connector };
  if (Object.keys(connection).length > 0) prodtrack[connector] = connection;
  if (Object.keys(profileOverrides).length > 0) {
    prodtrack.profile = profileOverrides;
  }
  return { prodtrack };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Merge `patch` over `base` the way the backend merges profile overrides: objects
 * merge key by key, anything else (including a list, or `null`, which means "remove
 * this key") replaces.  Returns a new object.
 */
export function mergeOverrides(
  base: ProfileOverrides,
  patch: ProfileOverrides
): ProfileOverrides {
  const out: ProfileOverrides = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    const existing = out[key];
    out[key] =
      isPlainObject(existing) && isPlainObject(value)
        ? mergeOverrides(existing, value)
        : value;
  }
  return out;
}

/** The suggestions that carry a ready-to-apply override. */
export function applicableSuggestions(
  report: HandshakeReport | null
): HandshakeSuggestion[] {
  return (report?.suggestions ?? []).filter((s) => s.override !== null);
}

export interface ReportSummary {
  ok: number;
  warn: number;
  fail: number;
  skip: number;
}

export function summarizeChecks(report: HandshakeReport): ReportSummary {
  const summary: ReportSummary = { ok: 0, warn: 0, fail: 0, skip: 0 };
  for (const check of report.checks) summary[check.status] += 1;
  return summary;
}

/** May the candidate be saved?  Only a report with no failing check. */
export function canSave(report: HandshakeReport | null): boolean {
  return report !== null && report.ok && report.checks.length > 0;
}

export function isAdminRoute(search: string): boolean {
  return new URLSearchParams(search).get('admin') === 'config';
}
