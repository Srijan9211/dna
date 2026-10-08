import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AlertDialog,
  Badge,
  Button,
  Callout,
  Card,
  Code,
  Flex,
  Heading,
  Spinner,
  Table,
  Text,
  TextField,
} from '@radix-ui/themes';
import type { ProjectOverride, ProjectSetting } from '@dna/core';
import { apiHandler, useProjectOverrides } from '../../api';
import { describeAdminError, type AdminError } from './adminErrors';
import {
  buildProjectPatch,
  groupByProject,
  isRemovable,
  isValidProjectId,
  parseSettingValue,
  settingKey,
  showValue,
} from './projectOverrides';

export interface KnownProject {
  id: string;
  name: string;
}

interface Props {
  /** The administrator's own projects, offered as suggestions (any id may be typed). */
  projects?: KnownProject[];
}

const fieldStyle: React.CSSProperties = {
  padding: '6px 8px',
  borderRadius: 'var(--radius-2)',
  border: '1px solid var(--gray-7)',
  background: 'var(--color-surface)',
  color: 'var(--gray-12)',
  font: 'inherit',
  width: '100%',
};

function Label({ htmlFor, children }: { htmlFor: string; children: string }) {
  return (
    <Text as="label" htmlFor={htmlFor} size="2" weight="medium">
      {children}
    </Text>
  );
}

interface Outcome {
  revision: number;
  restart: string[];
  message: string;
}

/**
 * Settings that differ for one project.  Most of DNA is configured site-wide; only the
 * few settings the backend marks as project-scoped can be overridden here, so a studio
 * can, for example, publish transcripts for one show without enabling them everywhere.
 */
export function ProjectOverridesPanel({ projects = [] }: Props) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useProjectOverrides();

  const [project, setProject] = useState('');
  const [settingChoice, setSettingChoice] = useState('');
  const [raw, setRaw] = useState('');
  const [note, setNote] = useState('');
  const [failure, setFailure] = useState<AdminError | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const settings = data?.settings ?? [];
  const chosen: ProjectSetting | undefined =
    settings.find((s) => settingKey(s) === settingChoice) ?? settings[0];

  const projectId = project.trim();
  const projectOk = isValidProjectId(projectId);
  const parsed = chosen ? parseSettingValue(chosen.type, raw) : null;

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['config'] });

  const save = useMutation({
    mutationFn: () => {
      if (!chosen || !parsed || !parsed.ok) throw new Error('Nothing to save');
      return apiHandler.setProjectOverrides(
        projectId,
        buildProjectPatch(chosen.section, chosen.path, parsed.value),
        { note, expectedVersion: data?.revision }
      );
    },
    onSuccess: async (result) => {
      setFailure(null);
      setOutcome({
        revision: result.revision,
        restart: result.restart_required,
        message: `Saved ${projectId} · ${chosen?.path}`,
      });
      setRaw('');
      setNote('');
      await refresh();
    },
    onError: (err) => {
      setOutcome(null);
      setFailure(describeAdminError(err));
    },
  });

  const remove = useMutation({
    mutationFn: (o: ProjectOverride) =>
      apiHandler.removeProjectOverride(o.project, {
        section: o.section,
        path: o.path,
        expectedVersion: data?.revision,
      }),
    onSuccess: async (result, o) => {
      setFailure(null);
      setOutcome({
        revision: result.revision,
        restart: result.restart_required,
        message: `Removed ${o.project} · ${o.path}; the site-wide value applies again`,
      });
      await refresh();
    },
    onError: (err) => {
      setOutcome(null);
      setFailure(describeAdminError(err));
    },
  });

  const groups = useMemo(
    () => groupByProject(data?.overrides ?? []),
    [data?.overrides]
  );
  const siteValue = (o: ProjectOverride) =>
    settings.find((s) => s.section === o.section && s.path === o.path)
      ?.site_value;

  if (isLoading) return <Spinner />;
  if (error || !data) {
    return (
      <Callout.Root color="red" role="alert">
        <Callout.Text>Could not load the per-project settings.</Callout.Text>
      </Callout.Root>
    );
  }

  const canSave =
    !!chosen && projectOk && !!parsed && parsed.ok && !save.isPending;

  return (
    <Flex direction="column" gap="4">
      <Text size="2" color="gray">
        Everything is configured site-wide. The few settings listed here can
        also be set differently for one project; a project without an override
        uses the site-wide value.
      </Text>

      {failure && (
        <Callout.Root color="red" role="alert">
          <Callout.Text>{failure.message}</Callout.Text>
          {failure.issues.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 20 }}>
              {failure.issues.map((issue, i) => (
                <li key={i}>
                  <Text size="1">
                    {issue.path || issue.section}: {issue.message}
                  </Text>
                </li>
              ))}
            </ul>
          )}
          {failure.stale && (
            <Text size="1" as="p">
              Someone saved a change since this page loaded. Reload to see it,
              then try again.
            </Text>
          )}
        </Callout.Root>
      )}
      {outcome && (
        <Callout.Root color="green" role="status">
          <Callout.Text>
            {outcome.message}. Saved as revision {outcome.revision}.
            {outcome.restart.length > 0
              ? ` Restart the backend to apply: ${outcome.restart.join(', ')}.`
              : ''}
          </Callout.Text>
        </Callout.Root>
      )}

      <Card>
        <Flex direction="column" gap="3">
          <Heading size="3">Set an override</Heading>
          {settings.length === 0 ? (
            <Text size="2" color="gray">
              No setting can be overridden per project.
            </Text>
          ) : (
            <>
              <Flex direction="column" gap="1">
                <Label htmlFor="po-project">Project</Label>
                <TextField.Root
                  id="po-project"
                  placeholder="The project's id in your tracker"
                  list="po-projects"
                  value={project}
                  onChange={(e) => setProject(e.target.value)}
                  color={project !== '' && !projectOk ? 'red' : undefined}
                />
                <datalist id="po-projects">
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </datalist>
                {project !== '' && !projectOk && (
                  <Text size="1" color="red" role="alert">
                    Use letters, numbers and . _ : - only.
                  </Text>
                )}
              </Flex>

              <Flex direction="column" gap="1">
                <Label htmlFor="po-setting">Setting</Label>
                <select
                  id="po-setting"
                  style={fieldStyle}
                  value={chosen ? settingKey(chosen) : ''}
                  onChange={(e) => {
                    setSettingChoice(e.target.value);
                    setRaw('');
                  }}
                >
                  {settings.map((s) => (
                    <option key={settingKey(s)} value={settingKey(s)}>
                      {settingKey(s)}
                    </option>
                  ))}
                </select>
                {chosen && (
                  <Text size="1" color="gray">
                    {chosen.description}
                    {chosen.description ? ' ' : ''}Site-wide value:{' '}
                    <Code variant="ghost">{showValue(chosen.site_value)}</Code>
                    {chosen.restart
                      ? ' (needs a backend restart to apply)'
                      : ''}
                  </Text>
                )}
              </Flex>

              {chosen && (
                <Flex direction="column" gap="1">
                  <Label htmlFor="po-value">Value for this project</Label>
                  {chosen.type === 'boolean' ? (
                    <select
                      id="po-value"
                      style={fieldStyle}
                      value={raw}
                      onChange={(e) => setRaw(e.target.value)}
                    >
                      <option value="">Choose…</option>
                      <option value="true">On</option>
                      <option value="false">Off</option>
                    </select>
                  ) : (
                    <TextField.Root
                      id="po-value"
                      value={raw}
                      onChange={(e) => setRaw(e.target.value)}
                    />
                  )}
                  {parsed && !parsed.ok && raw !== '' && (
                    <Text size="1" color="red" role="alert">
                      {parsed.error}
                    </Text>
                  )}
                </Flex>
              )}

              <Flex direction="column" gap="1">
                <Label htmlFor="po-note">Note (shown in History)</Label>
                <TextField.Root
                  id="po-note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </Flex>

              <Flex justify="end">
                <Button onClick={() => save.mutate()} disabled={!canSave}>
                  {save.isPending ? <Spinner /> : 'Save override'}
                </Button>
              </Flex>
            </>
          )}
        </Flex>
      </Card>

      <Flex direction="column" gap="2">
        <Heading size="3">Current overrides</Heading>
        {groups.length === 0 ? (
          <Text size="2" color="gray">
            No project has its own settings yet.
          </Text>
        ) : (
          groups.map((group) => (
            <Card key={group.project}>
              <Flex direction="column" gap="2">
                <Heading size="2">{group.project}</Heading>
                <Table.Root size="1" variant="ghost">
                  <Table.Header>
                    <Table.Row>
                      <Table.ColumnHeaderCell>Setting</Table.ColumnHeaderCell>
                      <Table.ColumnHeaderCell>Site-wide</Table.ColumnHeaderCell>
                      <Table.ColumnHeaderCell>
                        This project
                      </Table.ColumnHeaderCell>
                      <Table.ColumnHeaderCell>Set in</Table.ColumnHeaderCell>
                      <Table.ColumnHeaderCell />
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {group.items.map((o) => (
                      <Table.Row key={`${o.section}.${o.path}`}>
                        <Table.RowHeaderCell>
                          <Code variant="ghost">{settingKey(o)}</Code>
                        </Table.RowHeaderCell>
                        <Table.Cell>{showValue(siteValue(o))}</Table.Cell>
                        <Table.Cell>
                          <Text weight="medium">{showValue(o.value)}</Text>
                        </Table.Cell>
                        <Table.Cell>
                          <Badge color={isRemovable(o) ? 'blue' : 'gray'}>
                            {isRemovable(o) ? 'admin page' : 'config file'}
                          </Badge>
                        </Table.Cell>
                        <Table.Cell>
                          {isRemovable(o) ? (
                            <AlertDialog.Root>
                              <AlertDialog.Trigger>
                                <Button
                                  size="1"
                                  variant="soft"
                                  color="gray"
                                  aria-label={`Remove ${settingKey(o)} override for ${o.project}`}
                                >
                                  Remove
                                </Button>
                              </AlertDialog.Trigger>
                              <AlertDialog.Content maxWidth="440px">
                                <AlertDialog.Title>
                                  Remove this override?
                                </AlertDialog.Title>
                                <AlertDialog.Description size="2">
                                  {o.project} goes back to the site-wide value
                                  for <Code>{settingKey(o)}</Code>. This is
                                  recorded in History and can be rolled back.
                                </AlertDialog.Description>
                                <Flex gap="3" mt="4" justify="end">
                                  <AlertDialog.Cancel>
                                    <Button variant="soft" color="gray">
                                      Cancel
                                    </Button>
                                  </AlertDialog.Cancel>
                                  <AlertDialog.Action>
                                    <Button onClick={() => remove.mutate(o)}>
                                      Remove
                                    </Button>
                                  </AlertDialog.Action>
                                </Flex>
                              </AlertDialog.Content>
                            </AlertDialog.Root>
                          ) : (
                            <Text size="1" color="gray">
                              edit the file
                            </Text>
                          )}
                        </Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </Table.Root>
              </Flex>
            </Card>
          ))
        )}
      </Flex>
    </Flex>
  );
}
