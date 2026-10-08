import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Badge,
  Button,
  Callout,
  Card,
  Code,
  Dialog,
  Flex,
  Heading,
  Spinner,
  Table,
  Text,
  TextArea,
} from '@radix-ui/themes';
import { Lock } from 'lucide-react';
import type { ConfigIssue, EffectiveConfig } from '@dna/core';
import { apiHandler, useEffectiveConfig } from '../../api';
import {
  describeAdminError,
  parseJsonObject,
  withoutMaskedSecrets,
  type AdminError,
} from './adminErrors';
import { describeSource, flattenConfig } from './configView';

interface EditorProps {
  section: string;
  values: Record<string, unknown>;
  version: number;
  onClose: () => void;
}

/**
 * Edit one section as JSON.  Validation runs on the server (the same rules as a real
 * save) so the editor can never accept something the backend would refuse, and a
 * secret can only be given as a reference.
 */
function SectionEditor({ section, values, version, onClose }: EditorProps) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(() =>
    JSON.stringify(withoutMaskedSecrets(values), null, 2)
  );
  const [note, setNote] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [issues, setIssues] = useState<ConfigIssue[]>([]);
  const [preview, setPreview] = useState<{
    changed: string[];
    restart: string[];
  } | null>(null);
  const [error, setError] = useState<AdminError | null>(null);

  const parsed = parseJsonObject(text);

  const validate = useMutation({
    mutationFn: (value: Record<string, unknown>) =>
      apiHandler.validateConfig({ [section]: value }),
  });
  const save = useMutation({
    mutationFn: (value: Record<string, unknown>) =>
      apiHandler.updateConfigSection(section, value, {
        note,
        expectedVersion: version,
      }),
  });

  const onValidate = async () => {
    setError(null);
    setPreview(null);
    setIssues([]);
    if (!parsed.ok) {
      setProblem(parsed.error);
      return;
    }
    setProblem(null);
    try {
      const result = await validate.mutateAsync(parsed.value);
      if (result.valid) {
        setPreview({
          changed: result.changed,
          restart: result.restart_required,
        });
      } else {
        setIssues(result.issues);
      }
    } catch (err) {
      setError(describeAdminError(err));
    }
  };

  const onSave = async () => {
    if (!parsed.ok) return;
    setError(null);
    try {
      await save.mutateAsync(parsed.value);
      await queryClient.invalidateQueries({ queryKey: ['config'] });
      onClose();
    } catch (err) {
      const described = describeAdminError(err);
      setIssues(described.issues);
      setError(described);
    }
  };

  return (
    <Flex direction="column" gap="3">
      <Text size="2" color="gray">
        Changes to <Code>{section}</Code> are checked by the server before they
        are saved, recorded with your name, and can be rolled back from History.
        Secrets must be references such as <Code>{'${env:NAME}'}</Code>.
      </Text>
      <TextArea
        aria-label={`${section} settings (JSON)`}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
        rows={16}
        spellCheck={false}
        style={{ fontFamily: 'var(--code-font-family)' }}
      />
      {problem && (
        <Text size="2" color="red" role="alert">
          {problem}
        </Text>
      )}
      {issues.length > 0 && (
        <Callout.Root color="red" role="alert">
          <Callout.Text>The server refused this change:</Callout.Text>
          <ul style={{ margin: '6px 0 0', paddingLeft: 20 }}>
            {issues.map((issue, i) => (
              <li key={i}>
                <Text size="1">
                  {issue.path || issue.section}: {issue.message}
                </Text>
              </li>
            ))}
          </ul>
        </Callout.Root>
      )}
      {error && issues.length === 0 && (
        <Callout.Root color="red" role="alert">
          <Callout.Text>
            {error.message}
            {error.stale ? ' Reload to see the latest, then try again.' : ''}
          </Callout.Text>
        </Callout.Root>
      )}
      {preview && (
        <Callout.Root color="green" role="status">
          <Callout.Text>
            {preview.changed.length === 0
              ? 'Valid. This changes nothing.'
              : `Valid. Changes: ${preview.changed.join(', ')}.`}
            {preview.restart.length > 0
              ? ` Needs a backend restart: ${preview.restart.join(', ')}.`
              : ''}
          </Callout.Text>
        </Callout.Root>
      )}
      <TextArea
        aria-label="Note"
        placeholder="Why this change? (shown in History)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={2}
      />
      <Flex gap="3" justify="end">
        <Button variant="soft" color="gray" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="soft"
          onClick={() => void onValidate()}
          disabled={validate.isPending}
        >
          {validate.isPending ? <Spinner /> : 'Validate'}
        </Button>
        <Button
          onClick={() => void onSave()}
          disabled={!parsed.ok || save.isPending}
        >
          {save.isPending ? <Spinner /> : 'Save'}
        </Button>
      </Flex>
    </Flex>
  );
}

function SectionCard({
  name,
  data,
  onEdit,
}: {
  name: string;
  data: EffectiveConfig;
  onEdit: () => void;
}) {
  const locked = data.locked_sections.includes(name);
  const rows = flattenConfig(name, data.config[name]);
  return (
    <Card>
      <Flex direction="column" gap="2">
        <Flex justify="between" align="center">
          <Flex gap="2" align="center">
            <Heading size="3">{name}</Heading>
            {locked && (
              <Badge
                color="gray"
                title="Set in the config files or environment only"
              >
                <Lock size={11} aria-hidden /> locked
              </Badge>
            )}
          </Flex>
          {!locked && (
            <Button size="1" variant="soft" onClick={onEdit}>
              Edit
            </Button>
          )}
        </Flex>
        <Table.Root size="1" variant="ghost">
          <Table.Body>
            {rows.map((row) => (
              <Table.Row key={row.path}>
                <Table.RowHeaderCell>
                  <Code variant="ghost">{row.path.slice(name.length + 1)}</Code>
                </Table.RowHeaderCell>
                <Table.Cell style={{ wordBreak: 'break-all' }}>
                  {row.value}
                </Table.Cell>
                <Table.Cell>
                  <Text size="1" color="gray">
                    {describeSource(data.provenance[row.path])}
                  </Text>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Flex>
    </Card>
  );
}

export function SettingsPanel() {
  const { data, isLoading, error } = useEffectiveConfig();
  const [editing, setEditing] = useState<string | null>(null);

  if (isLoading) return <Spinner />;
  if (error || !data) {
    return (
      <Callout.Root color="red" role="alert">
        <Callout.Text>Could not load the configuration.</Callout.Text>
      </Callout.Root>
    );
  }

  return (
    <Flex direction="column" gap="3">
      <Text size="2" color="gray">
        The configuration in effect, and where each value comes from. Locked
        sections hold security and start-up settings and are changed in the
        config files or environment, never from here.
      </Text>
      {Object.keys(data.config).map((name) => (
        <SectionCard
          key={name}
          name={name}
          data={data}
          onEdit={() => setEditing(name)}
        />
      ))}

      <Dialog.Root
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      >
        <Dialog.Content maxWidth="720px">
          <Dialog.Title>Edit {editing}</Dialog.Title>
          {editing && (
            <SectionEditor
              section={editing}
              values={data.config[editing] ?? {}}
              version={data.revision}
              onClose={() => setEditing(null)}
            />
          )}
        </Dialog.Content>
      </Dialog.Root>
    </Flex>
  );
}
