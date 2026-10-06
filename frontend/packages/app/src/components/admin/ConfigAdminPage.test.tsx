import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { configure, screen, waitFor, within } from '@testing-library/react';
import type { EffectiveConfig, HandshakeReport } from '@dna/core';
import { render } from '../../test/render';
import { apiHandler } from '../../api';
import { ConfigAdminPage } from './ConfigAdminPage';
import { HandshakeReportView } from './HandshakeReportView';
import { HistoryPanel } from './HistoryPanel';
import { SettingsPanel } from './SettingsPanel';

configure({ asyncUtilTimeout: 8000 });

const effective = (over: Partial<EffectiveConfig> = {}): EffectiveConfig => ({
  version: 1,
  revision: 2,
  config: {
    auth: { provider: 'none' },
    prodtrack: { connector: 'ayon', ayon: { url: 'http://ayon.test', api_key: '********' } },
    llm: { provider: 'openai', profiles: {} },
  },
  provenance: {
    'auth.provider': 'env:AUTH_PROVIDER',
    'prodtrack.connector': 'runtime',
    'prodtrack.ayon.url': 'env:AYON_URL',
    'prodtrack.ayon.api_key': 'env:AYON_API_KEY',
    'llm.provider': 'default',
  },
  runtime_overrides: {},
  locked_sections: ['auth'],
  ...over,
});

beforeEach(() => {
  vi.spyOn(apiHandler, 'getConfigAccess').mockResolvedValue({ admin: true });
  vi.spyOn(apiHandler, 'getEffectiveConfig').mockResolvedValue(effective());
  vi.spyOn(apiHandler, 'getConfigHistory').mockResolvedValue([]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ConfigAdminPage access', () => {
  it('shows the admin pages to a configuration administrator', async () => {
    render(<ConfigAdminPage />);
    expect(await screen.findByRole('heading', { name: 'Configuration' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Tracker setup/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Settings/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /History/ })).toBeInTheDocument();
    expect(await screen.findByText(/revision 2/)).toBeInTheDocument();
  });

  it('shows everyone else only a polite refusal, and never loads the settings', async () => {
    vi.mocked(apiHandler.getConfigAccess).mockResolvedValue({ admin: false });
    render(<ConfigAdminPage />);
    expect(await screen.findByText(/do not have access to configuration/i)).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /Settings/ })).toBeNull();
    expect(apiHandler.getEffectiveConfig).not.toHaveBeenCalled();
  });
});

describe('SettingsPanel', () => {
  it('shows each setting with where it came from, and masks secrets', async () => {
    render(<SettingsPanel />);
    expect(await screen.findByText('http://ayon.test')).toBeInTheDocument();
    expect(screen.getByText('environment AYON_URL')).toBeInTheDocument();
    expect(screen.getByText('saved in the admin page')).toBeInTheDocument();
    expect(screen.getByText('********')).toBeInTheDocument();
  });

  it('offers Edit for open sections and a lock for security sections', async () => {
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    expect(screen.getByText('locked')).toBeInTheDocument();
    // auth is locked: only prodtrack and llm get an Edit button
    expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
  });

  it('opens an editor that omits masked secrets so they are never sent back', async () => {
    const user = userEvent.setup();
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[0]);

    const editor = (await screen.findByLabelText('prodtrack settings (JSON)')) as HTMLTextAreaElement;
    const shown = JSON.parse(editor.value);
    expect(shown.ayon.url).toBe('http://ayon.test');
    expect('api_key' in shown.ayon).toBe(false);
    expect(editor.value).not.toContain('********');
  });

  it("shows the server's reasons when validation fails, and keeps the dialog open", async () => {
    vi.spyOn(apiHandler, 'validateConfig').mockResolvedValue({
      valid: false,
      issues: [{ section: 'prodtrack', path: 'prodtrack.ayon.api_key', message: 'secret values must be a reference', source: 'runtime' }],
      changed: [],
      restart_required: [],
    });
    const user = userEvent.setup();
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
    await user.click(await screen.findByRole('button', { name: 'Validate' }));

    expect(await screen.findByText(/prodtrack.ayon.api_key: secret values must be a reference/)).toBeInTheDocument();
    expect(apiHandler.validateConfig).toHaveBeenCalledWith({ prodtrack: expect.objectContaining({ connector: 'ayon' }) });
  });

  it('previews what a valid change would do without saving it', async () => {
    vi.spyOn(apiHandler, 'validateConfig').mockResolvedValue({
      valid: true, issues: [], changed: ['prodtrack.connector'], restart_required: ['prodtrack.connector'],
    });
    const update = vi.spyOn(apiHandler, 'updateConfigSection');
    const user = userEvent.setup();
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
    await user.click(await screen.findByRole('button', { name: 'Validate' }));

    expect(await screen.findByText(/Valid\. Changes: prodtrack.connector\./)).toHaveTextContent('Needs a backend restart');
    expect(update).not.toHaveBeenCalled();
  });

  it('saves with the note and the revision it was based on, then closes', async () => {
    const update = vi.spyOn(apiHandler, 'updateConfigSection').mockResolvedValue({ revision: 3, changed: [], restart_required: [] });
    const user = userEvent.setup();
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[1]); // llm
    await user.type(await screen.findByLabelText('Note'), 'try gemini');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update).toHaveBeenCalledWith('llm', { provider: 'openai', profiles: {} }, { note: 'try gemini', expectedVersion: 2 });
    await waitFor(() => expect(screen.queryByLabelText('llm settings (JSON)')).toBeNull());
  });

  it('refuses to save text that is not a JSON object, without calling the server', async () => {
    const update = vi.spyOn(apiHandler, 'updateConfigSection');
    const user = userEvent.setup();
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[1]);
    const editor = await screen.findByLabelText('llm settings (JSON)');
    await user.clear(editor);
    await user.click(editor);
    await user.paste('{ not json');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByText(/Not valid JSON/)).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });

  it('tells the admin to reload when someone else saved first', async () => {
    vi.spyOn(apiHandler, 'updateConfigSection').mockRejectedValue({
      response: { status: 409, data: { detail: 'expected revision 2, store is at 3' } },
    });
    const user = userEvent.setup();
    render(<SettingsPanel />);
    await screen.findByText('http://ayon.test');
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[1]);
    await user.click(await screen.findByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/store is at 3.*Reload to see the latest/)).toBeInTheDocument();
  });
});

describe('HistoryPanel', () => {
  const revisions = [
    { version: 3, actor: 'ann@x.com', at: 1_760_000_000, note: 'switch to AYON', changed: ['prodtrack.connector'] },
    { version: 2, actor: 'bob@x.com', at: 1_759_000_000, note: '', changed: [] },
  ];

  it('explains an empty history', async () => {
    render(<HistoryPanel />);
    expect(await screen.findByText(/No changes have been saved yet/)).toBeInTheDocument();
  });

  it('lists who changed what, marking the current revision', async () => {
    vi.mocked(apiHandler.getConfigHistory).mockResolvedValue(revisions);
    render(<HistoryPanel />);
    expect(await screen.findByText('ann@x.com')).toBeInTheDocument();
    expect(screen.getByText('switch to AYON')).toBeInTheDocument();
    expect(screen.getByText('prodtrack.connector')).toBeInTheDocument();
    expect(screen.getByText('no setting changed')).toBeInTheDocument();
    expect(screen.getByText('current')).toBeInTheDocument();
    // the current revision cannot be rolled back to; the older one can
    expect(screen.getAllByRole('button', { name: /Roll back to revision/ })).toHaveLength(1);
  });

  it('rolls back only after confirmation, as a new revision', async () => {
    vi.mocked(apiHandler.getConfigHistory).mockResolvedValue(revisions);
    const rollback = vi.spyOn(apiHandler, 'rollbackConfig').mockResolvedValue({ revision: 4, changed: [], restart_required: [] });
    const user = userEvent.setup();
    render(<HistoryPanel />);
    await user.click(await screen.findByRole('button', { name: 'Roll back to revision 2' }));
    expect(rollback).not.toHaveBeenCalled(); // asked first
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/saved as a\s+new revision/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Roll back' }));

    await waitFor(() => expect(rollback).toHaveBeenCalledWith(2));
    expect(await screen.findByText(/Rolled back\. Saved as revision 4\./)).toBeInTheDocument();
  });

  it('shows why a rollback failed', async () => {
    vi.mocked(apiHandler.getConfigHistory).mockResolvedValue(revisions);
    vi.spyOn(apiHandler, 'rollbackConfig').mockRejectedValue({
      response: { status: 422, data: { detail: { message: 'Invalid configuration', issues: [] } } },
    });
    const user = userEvent.setup();
    render(<HistoryPanel />);
    await user.click(await screen.findByRole('button', { name: 'Roll back to revision 2' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Roll back' }));
    expect(await screen.findByText('Invalid configuration')).toBeInTheDocument();
  });
});

describe('HandshakeReportView', () => {
  const base: HandshakeReport = {
    connector: 'ayon', contract: 'dna.prodtrack/1', ok: true, applied: false,
    checks: [], suggestions: [], capabilities: {}, schema: {},
  };

  it('says plainly when it is safe to save, and when warnings remain', () => {
    const { rerender } = render(
      <HandshakeReportView report={{ ...base, checks: [{ name: 'a', status: 'ok', message: 'fine', details: {} }] }} />
    );
    expect(screen.getByText('Everything checks out. Ready to save.')).toBeInTheDocument();
    rerender(
      <HandshakeReportView report={{ ...base, checks: [{ name: 'a', status: 'warn', message: 'careful', details: {} }] }} />
    );
    expect(screen.getByText('Ready to save, with 1 warning.')).toBeInTheDocument();
  });

  it('counts failures and labels each check by status', () => {
    render(
      <HandshakeReportView
        report={{ ...base, ok: false, checks: [
          { name: 'connect', status: 'ok', message: 'up', details: {} },
          { name: 'mapping.shot', status: 'fail', message: 'no such field', details: {} },
          { name: 'mapping.task', status: 'fail', message: 'no such entity', details: {} },
          { name: 'discover', status: 'skip', message: 'cannot introspect', details: {} },
        ] }}
      />
    );
    expect(screen.getByText(/2 checks failed\. Fix them before saving\./)).toBeInTheDocument();
    expect(screen.getAllByLabelText('Failed')).toHaveLength(2);
    expect(screen.getByLabelText('Skipped')).toBeInTheDocument();
    expect(screen.getByLabelText('Passed')).toBeInTheDocument();
  });

  it('offers a fix button only for suggestions that carry a ready override', async () => {
    const onAccept = vi.fn();
    const user = userEvent.setup();
    render(
      <HandshakeReportView
        onAccept={onAccept}
        report={{ ...base, ok: false, checks: [{ name: 'm', status: 'fail', message: 'x', details: {} }], suggestions: [
          { path: 'a', problem: 'fixable', options: ['x'], override: { a: 1 } },
          { path: 'b', problem: 'manual', options: ['y'], override: null },
        ] }}
      />
    );
    const cards = screen.getAllByTestId('suggestion');
    expect(within(cards[0]).getByRole('button')).toBeInTheDocument();
    expect(within(cards[1]).queryByRole('button')).toBeNull();
    await user.click(within(cards[0]).getByRole('button'));
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ path: 'a' }));
  });

  it('shows no fix buttons when nothing can accept them (a stale report)', () => {
    render(
      <HandshakeReportView
        report={{ ...base, ok: false, checks: [], suggestions: [{ path: 'a', problem: 'p', options: [], override: { a: 1 } }] }}
      />
    );
    expect(screen.queryByRole('button')).toBeNull();
  });
});
