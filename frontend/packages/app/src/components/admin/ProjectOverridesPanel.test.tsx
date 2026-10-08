import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { configure, screen, waitFor, within } from '@testing-library/react';
import type { ProjectOverrides } from '@dna/core';
import { render } from '../../test/render';
import { apiHandler } from '../../api';
import { ProjectOverridesPanel } from './ProjectOverridesPanel';

configure({ asyncUtilTimeout: 8000 });

const data = (over: Partial<ProjectOverrides> = {}): ProjectOverrides => ({
  revision: 4,
  settings: [
    {
      section: 'features',
      path: 'transcript_publish',
      description: 'Publish meeting transcripts to the tracker.',
      type: 'boolean',
      site_value: false,
      restart: false,
      secret: false,
    },
    {
      section: 'prodtrack',
      path: 'shotgrid.transcript_entity',
      description: 'Custom-entity slot that stores transcripts.',
      type: 'string',
      site_value: 'CustomEntity01',
      restart: false,
      secret: false,
    },
  ],
  overrides: [],
  ...over,
});

let load: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  load = vi.spyOn(apiHandler, 'getProjectOverrides').mockResolvedValue(data());
});

afterEach(() => {
  vi.restoreAllMocks();
});

const saveButton = () => screen.getByRole('button', { name: 'Save override' });

describe('ProjectOverridesPanel', () => {
  it('explains the empty state', async () => {
    render(<ProjectOverridesPanel />);
    expect(
      await screen.findByText('No project has its own settings yet.')
    ).toBeInTheDocument();
  });

  it('shows each override beside the site-wide value, and where it was set', async () => {
    load.mockResolvedValue(
      data({
        overrides: [
          {
            project: 'show_a',
            section: 'features',
            path: 'transcript_publish',
            value: true,
            source: 'runtime',
          },
          {
            project: 'show_a',
            section: 'prodtrack',
            path: 'shotgrid.transcript_entity',
            value: 'CustomEntity05',
            source: 'file:/etc/dna/site.yaml',
          },
        ],
      })
    );
    render(<ProjectOverridesPanel />);

    const heading = await screen.findByRole('heading', { name: 'show_a' });
    const card = heading.closest('div')!.parentElement!;
    expect(within(card).getByText('on')).toBeInTheDocument(); // this project
    expect(within(card).getByText('off')).toBeInTheDocument(); // site-wide
    expect(within(card).getByText('CustomEntity05')).toBeInTheDocument();
    expect(within(card).getByText('admin page')).toBeInTheDocument();
    expect(within(card).getByText('config file')).toBeInTheDocument();
    // only the one set here can be removed here
    expect(
      screen.getAllByRole('button', { name: /^Remove .* override for show_a$/ })
    ).toHaveLength(1);
    expect(screen.getByText('edit the file')).toBeInTheDocument();
  });

  it('cannot save until the project and the value are both valid', async () => {
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');
    expect(saveButton()).toBeDisabled();

    await user.type(screen.getByLabelText('Project'), 'show a'); // a space
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /letters, numbers/
    );
    await user.selectOptions(
      screen.getByLabelText('Value for this project'),
      'true'
    );
    expect(saveButton()).toBeDisabled();

    await user.clear(screen.getByLabelText('Project'));
    await user.type(screen.getByLabelText('Project'), 'show_a');
    expect(saveButton()).toBeEnabled();
  });

  it('shows the site-wide value for the chosen setting', async () => {
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');
    expect(screen.getByText(/Site-wide value:/)).toHaveTextContent('off');
    await user.selectOptions(
      screen.getByLabelText('Setting'),
      'prodtrack.shotgrid.transcript_entity'
    );
    expect(screen.getByText(/Site-wide value:/)).toHaveTextContent(
      'CustomEntity01'
    );
  });

  it('saves a typed value as the right nested override, against the loaded revision', async () => {
    const save = vi.spyOn(apiHandler, 'setProjectOverrides').mockResolvedValue({
      revision: 5,
      changed: ['projects.show_a.prodtrack.shotgrid.transcript_entity'],
      restart_required: [],
    });
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');

    await user.type(screen.getByLabelText('Project'), 'show_a');
    await user.selectOptions(
      screen.getByLabelText('Setting'),
      'prodtrack.shotgrid.transcript_entity'
    );
    await user.type(
      screen.getByLabelText('Value for this project'),
      'CustomEntity05'
    );
    await user.type(screen.getByLabelText(/^Note/), 'show uses slot 5');
    await user.click(saveButton());

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(
      'show_a',
      { prodtrack: { shotgrid: { transcript_entity: 'CustomEntity05' } } },
      { note: 'show uses slot 5', expectedVersion: 4 }
    );
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Saved as revision 5'
    );
    // the fields are cleared and the list is reloaded
    expect(screen.getByLabelText('Value for this project')).toHaveValue('');
    await waitFor(() => expect(load.mock.calls.length).toBeGreaterThan(1));
  });

  it('saves a boolean as true or false, not as text', async () => {
    const save = vi
      .spyOn(apiHandler, 'setProjectOverrides')
      .mockResolvedValue({ revision: 5, changed: [], restart_required: [] });
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');
    await user.type(screen.getByLabelText('Project'), '1234');
    await user.selectOptions(
      screen.getByLabelText('Value for this project'),
      'true'
    );
    await user.click(saveButton());
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][1]).toEqual({
      features: { transcript_publish: true },
    });
  });

  it('says when a change needs a restart', async () => {
    vi.spyOn(apiHandler, 'setProjectOverrides').mockResolvedValue({
      revision: 6,
      changed: ['projects.p.x'],
      restart_required: ['projects.p.x'],
    });
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');
    await user.type(screen.getByLabelText('Project'), 'p');
    await user.selectOptions(
      screen.getByLabelText('Value for this project'),
      'false'
    );
    await user.click(saveButton());
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Restart the backend to apply: projects.p.x'
    );
  });

  it("shows the server's reasons when it refuses a change", async () => {
    vi.spyOn(apiHandler, 'setProjectOverrides').mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            message: 'Invalid configuration',
            issues: [
              {
                section: 'features',
                path: 'projects.p.features.x',
                message: 'cannot be overridden per project',
                source: 'runtime',
              },
            ],
          },
        },
      },
    });
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');
    await user.type(screen.getByLabelText('Project'), 'p');
    await user.selectOptions(
      screen.getByLabelText('Value for this project'),
      'true'
    );
    await user.click(saveButton());
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Invalid configuration');
    expect(alert).toHaveTextContent('cannot be overridden per project');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('tells the admin to reload when someone else saved first', async () => {
    vi.spyOn(apiHandler, 'setProjectOverrides').mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Configuration changed since you loaded it.' },
      },
    });
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await screen.findByText('Set an override');
    await user.type(screen.getByLabelText('Project'), 'p');
    await user.selectOptions(
      screen.getByLabelText('Value for this project'),
      'true'
    );
    await user.click(saveButton());
    expect(await screen.findByRole('alert')).toHaveTextContent(/Reload/);
  });

  it('removes an override only after confirmation', async () => {
    load.mockResolvedValue(
      data({
        overrides: [
          {
            project: 'show_a',
            section: 'features',
            path: 'transcript_publish',
            value: true,
            source: 'runtime',
          },
        ],
      })
    );
    const remove = vi
      .spyOn(apiHandler, 'removeProjectOverride')
      .mockResolvedValue({ revision: 5, changed: [], restart_required: [] });
    const user = userEvent.setup();
    render(<ProjectOverridesPanel />);
    await user.click(
      await screen.findByRole('button', {
        name: 'Remove features.transcript_publish override for show_a',
      })
    );
    expect(remove).not.toHaveBeenCalled(); // asked first
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toHaveTextContent('goes back to the site-wide value');
    await user.click(within(dialog).getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
    expect(remove).toHaveBeenCalledWith('show_a', {
      section: 'features',
      path: 'transcript_publish',
      expectedVersion: 4,
    });
    expect(await screen.findByRole('status')).toHaveTextContent(
      'site-wide value applies again'
    );
  });

  it('says so when the server cannot be read', async () => {
    load.mockRejectedValue(new Error('down'));
    render(<ProjectOverridesPanel />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load the per-project settings.'
    );
  });

  it('offers the admin’s own projects as suggestions but accepts any id', async () => {
    render(
      <ProjectOverridesPanel
        projects={[
          { id: 'dna_test', name: 'DNA Test' },
          { id: 'blood', name: 'Blood' },
        ]}
      />
    );
    await screen.findByText('Set an override');
    const options = document.querySelectorAll('#po-projects option');
    expect([...options].map((o) => o.getAttribute('value'))).toEqual([
      'dna_test',
      'blood',
    ]);
  });
});
