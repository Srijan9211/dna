import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockInstance,
} from 'vitest';
import userEvent from '@testing-library/user-event';
import { configure, screen, waitFor } from '@testing-library/react';
import type { HandshakeReport, HandshakeRequest } from '@dna/core';
import { render } from '../../test/render';
import { apiHandler } from '../../api';
import { TrackerSetup } from './TrackerSetup';

configure({ asyncUtilTimeout: 8000 });

const report = (over: Partial<HandshakeReport> = {}): HandshakeReport => ({
  connector: 'ayon',
  contract: 'dna.prodtrack/1',
  ok: true,
  checks: [
    {
      name: 'connect',
      status: 'ok',
      message: 'connected and authenticated',
      details: { server_version: '1.16.7' },
    },
    {
      name: 'smoke',
      status: 'ok',
      message: 'read 6 version(s) through the full path',
      details: {},
    },
  ],
  suggestions: [],
  capabilities: {},
  schema: {},
  applied: false,
  ...over,
});

const failing = (): HandshakeReport =>
  report({
    ok: false,
    checks: [
      {
        name: 'connect',
        status: 'ok',
        message: 'connected and authenticated',
        details: {},
      },
      {
        name: 'mapping.shot',
        status: 'fail',
        message: "shot: not found in the tracker: field 'attrib.descripton'",
        details: {},
      },
    ],
    suggestions: [
      {
        path: 'entities.shot.fields',
        problem: "'folder' has no field 'attrib.descripton'",
        options: ['attrib.description'],
        override: {
          entities: {
            shot: {
              fields: {
                'attrib.descripton': null,
                'attrib.description': 'description',
              },
            },
          },
        },
      },
    ],
  });

let run: MockInstance<(request: HandshakeRequest) => Promise<HandshakeReport>>;

beforeEach(() => {
  run = vi.spyOn(apiHandler, 'runHandshake');
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function fillAyon(
  user: ReturnType<typeof userEvent.setup>,
  key = '${env:AYON_API_KEY}'
) {
  await user.type(screen.getByLabelText(/Server URL/), 'http://ayon.test:5000');
  await user.click(screen.getByLabelText(/Service-user API key/));
  await user.paste(key);
}

const testButton = () =>
  screen.getByRole('button', { name: 'Test connection' });
const saveButton = () =>
  screen.getByRole('button', { name: 'Save configuration' });

describe('TrackerSetup', () => {
  it('cannot test until the required connection settings are filled in', async () => {
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    expect(testButton()).toBeDisabled();
    await user.type(
      screen.getByLabelText(/Server URL/),
      'http://ayon.test:5000'
    );
    expect(testButton()).toBeDisabled(); // the key is still missing
  });

  it('refuses a pasted secret and tells the admin what to enter instead', async () => {
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user, 'the-actual-key-value');
    expect(await screen.findByRole('alert')).toHaveTextContent(/reference/);
    expect(testButton()).toBeDisabled();
    expect(run).not.toHaveBeenCalled();
  });

  it('tests exactly what is on screen, never applying', async () => {
    run.mockResolvedValue(report());
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    await user.click(testButton());

    await waitFor(() => expect(run).toHaveBeenCalledTimes(1));
    expect(run).toHaveBeenCalledWith({
      candidate: {
        prodtrack: {
          connector: 'ayon',
          ayon: {
            url: 'http://ayon.test:5000',
            api_key: '${env:AYON_API_KEY}',
          },
        },
      },
      smoke_email: undefined,
      apply: false,
      note: '',
    });
    expect(
      await screen.findByText('Everything checks out. Ready to save.')
    ).toBeInTheDocument();
    expect(screen.getByText('server version: 1.16.7')).toBeInTheDocument();
  });

  it('shows what failed and offers the fix, and will not save a failing result', async () => {
    run.mockResolvedValue(failing());
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    await user.click(testButton());

    expect(await screen.findByText(/1 check failed/)).toBeInTheDocument();
    // named in both the failed check and the suggestion
    expect(
      screen.getAllByText(/attrib.descripton/).length
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('attrib.description')).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it('applying a suggestion merges its override and re-tests with it', async () => {
    run.mockResolvedValueOnce(failing()).mockResolvedValueOnce(report());
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    await user.click(testButton());
    await user.click(
      await screen.findByRole('button', { name: 'Apply this fix and re-test' })
    );

    await waitFor(() => expect(run).toHaveBeenCalledTimes(2));
    expect(run.mock.calls[1][0].candidate).toEqual({
      prodtrack: {
        connector: 'ayon',
        ayon: { url: 'http://ayon.test:5000', api_key: '${env:AYON_API_KEY}' },
        profile: {
          entities: {
            shot: {
              fields: {
                'attrib.descripton': null,
                'attrib.description': 'description',
              },
            },
          },
        },
      },
    });
    expect(
      await screen.findByText('Everything checks out. Ready to save.')
    ).toBeInTheDocument();
    await waitFor(() => expect(saveButton()).toBeEnabled());
  });

  it('saves only after a passing test, by re-running with apply', async () => {
    run.mockResolvedValueOnce(report()).mockResolvedValueOnce(
      report({
        applied: true,
        revision: 3,
        restart_required: ['prodtrack.connector'],
      })
    );
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    expect(saveButton()).toBeDisabled(); // nothing tested yet
    await user.click(testButton());
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await user.click(saveButton());

    await waitFor(() => expect(run).toHaveBeenCalledTimes(2));
    expect(run.mock.calls[1][0]).toMatchObject({
      apply: true,
      note: 'Configured AYON from the admin page',
    });
    expect(await screen.findByText(/Saved as revision 3/)).toHaveTextContent(
      'Restart the backend to apply: prodtrack.connector'
    );
  });

  it('invalidates a passing result as soon as anything is edited', async () => {
    run.mockResolvedValue(report());
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    await user.click(testButton());
    await waitFor(() => expect(saveButton()).toBeEnabled());

    await user.type(screen.getByLabelText(/Server URL/), '/changed');

    expect(saveButton()).toBeDisabled();
    expect(
      screen.getByText(/settings changed since this result/i)
    ).toBeInTheDocument();
  });

  it("shows the server's refusal when a failing result is saved anyway", async () => {
    const refused = failing();
    run.mockResolvedValueOnce(report()).mockRejectedValueOnce({
      response: {
        status: 409,
        data: {
          detail: {
            message: 'The handshake has failing checks; nothing was saved.',
            report: refused,
          },
        },
      },
    });
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    await user.click(testButton());
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await user.click(saveButton());

    expect(await screen.findByText(/nothing was saved/)).toBeInTheDocument();
    expect(await screen.findByText(/1 check failed/)).toBeInTheDocument(); // the report it carried
  });

  it('shows a rejected configuration as a list of problems', async () => {
    run.mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            message: 'Invalid configuration',
            issues: [
              {
                section: 'prodtrack',
                path: 'prodtrack.ayon.timeout',
                message: 'Input should be greater than 0',
                source: 'runtime',
              },
            ],
          },
        },
      },
    });
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="ayon" />);
    await fillAyon(user);
    await user.click(testButton());
    expect(
      await screen.findByText('Invalid configuration')
    ).toBeInTheDocument();
    expect(
      screen.getByText(/prodtrack.ayon.timeout: Input should be greater than 0/)
    ).toBeInTheDocument();
  });

  it('starts from the connector in effect and needs no connection for the mock', async () => {
    run.mockResolvedValue(report({ connector: 'mock' }));
    const user = userEvent.setup();
    render(<TrackerSetup currentConnector="mock" />);
    expect(screen.queryByLabelText(/Server URL/)).toBeNull();
    await waitFor(() => expect(testButton()).toBeEnabled());
    await user.click(testButton());
    await waitFor(() => expect(run).toHaveBeenCalled());
    expect(run.mock.calls[0][0].candidate).toEqual({
      prodtrack: { connector: 'mock' },
    });
  });
});
