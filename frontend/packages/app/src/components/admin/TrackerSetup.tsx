import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  Callout,
  Flex,
  Heading,
  Select,
  Spinner,
  Text,
  TextField,
} from '@radix-ui/themes';
import type { HandshakeReport, HandshakeSuggestion } from '@dna/core';
import { apiHandler } from '../../api';
import { useAuth } from '../../contexts';
import { HandshakeReportView } from './HandshakeReportView';
import { describeAdminError, type AdminError } from './adminErrors';
import {
  CONNECTORS,
  buildCandidate,
  canSave,
  connectorSpec,
  mergeOverrides,
  validateForm,
  type ConnectorId,
  type FormValues,
  type ProfileOverrides,
} from './trackerSetupLogic';

interface Props {
  /** The connector currently in effect, to start the form from. */
  currentConnector?: string;
}

function asConnector(value: string | undefined): ConnectorId {
  return CONNECTORS.some((c) => c.id === value) ? (value as ConnectorId) : 'ayon';
}

/**
 * Configure DNA for a production tracker: pick it, enter how to reach it, test the
 * connection against the real tracker, apply suggested fixes, and save only when
 * every check passes.  Nothing is written to the tracker; nothing is saved until the
 * test passes for exactly what is on screen.
 */
export function TrackerSetup({ currentConnector }: Props) {
  const { user } = useAuth();
  const queryClient = useQueryClient();

  const [connector, setConnector] = useState<ConnectorId>(
    asConnector(currentConnector)
  );
  const [values, setValues] = useState<FormValues>({});
  const [overrides, setOverrides] = useState<ProfileOverrides>({});
  const [smokeEmail, setSmokeEmail] = useState(user?.email ?? '');
  const [report, setReport] = useState<HandshakeReport | null>(null);
  // What the report was produced for, so an edit afterwards invalidates it.
  const [testedKey, setTestedKey] = useState<string | null>(null);
  const [error, setError] = useState<AdminError | null>(null);
  const [savedRevision, setSavedRevision] = useState<{
    revision: number;
    restart: string[];
  } | null>(null);

  const spec = connectorSpec(connector);
  const errors = useMemo(() => validateForm(connector, values), [connector, values]);
  const candidate = useMemo(
    () => buildCandidate(connector, values, overrides),
    [connector, values, overrides]
  );
  const key = JSON.stringify({ candidate, smokeEmail });
  const formValid = Object.keys(errors).length === 0;
  const isFresh = report !== null && testedKey === key;

  const run = useMutation({
    mutationFn: (args: { candidate: Record<string, unknown>; apply: boolean }) =>
      apiHandler.runHandshake({
        candidate: args.candidate,
        smoke_email: smokeEmail.trim() || undefined,
        apply: args.apply,
        note: args.apply ? `Configured ${spec.label} from the admin page` : '',
      }),
  });

  const test = async (nextCandidate = candidate) => {
    setError(null);
    setSavedRevision(null);
    try {
      const result = await run.mutateAsync({ candidate: nextCandidate, apply: false });
      setReport(result);
      setTestedKey(JSON.stringify({ candidate: nextCandidate, smokeEmail }));
    } catch (err) {
      setReport(null);
      setError(describeAdminError(err));
    }
  };

  const save = async () => {
    setError(null);
    try {
      const result = await run.mutateAsync({ candidate, apply: true });
      setReport(result);
      setSavedRevision({
        revision: result.revision ?? 0,
        restart: result.restart_required ?? [],
      });
      await queryClient.invalidateQueries({ queryKey: ['config'] });
    } catch (err) {
      const described = describeAdminError(err);
      if (described.report) setReport(described.report);
      setError(described);
    }
  };

  const accept = (suggestion: HandshakeSuggestion) => {
    if (suggestion.override === null) return;
    const nextOverrides = mergeOverrides(overrides, suggestion.override);
    setOverrides(nextOverrides);
    void test(buildCandidate(connector, values, nextOverrides));
  };

  const change = (field: string, value: string) =>
    setValues((previous) => ({ ...previous, [field]: value }));

  return (
    <Flex direction="column" gap="4">
      <Flex direction="column" gap="1">
        <Heading size="4">Production tracker</Heading>
        <Text size="2" color="gray">
          Choose your tracker and how DNA reaches it. DNA tests the real connection
          before anything is saved, and never stores a secret as plain text.
        </Text>
      </Flex>

      <Flex direction="column" gap="1">
        <Text as="label" size="2" weight="medium" htmlFor="connector">
          Tracker
        </Text>
        <Select.Root
          value={connector}
          onValueChange={(v) => {
            setConnector(v as ConnectorId);
            setValues({});
            setOverrides({});
            setReport(null);
            setError(null);
          }}
        >
          <Select.Trigger id="connector" aria-label="Tracker" />
          <Select.Content>
            {CONNECTORS.map((c) => (
              <Select.Item key={c.id} value={c.id}>
                {c.label}
              </Select.Item>
            ))}
          </Select.Content>
        </Select.Root>
        <Text size="1" color="gray">
          {spec.summary}
        </Text>
      </Flex>

      {spec.fields.map((field) => (
        <Flex key={`${connector}-${field.key}`} direction="column" gap="1">
          <Text as="label" size="2" weight="medium" htmlFor={`f-${field.key}`}>
            {field.label}
            {field.required ? ' *' : ''}
          </Text>
          {field.options ? (
            <Select.Root
              value={values[field.key] || field.options[0]}
              onValueChange={(v) => change(field.key, v)}
            >
              <Select.Trigger id={`f-${field.key}`} aria-label={field.label} />
              <Select.Content>
                {field.options.map((o) => (
                  <Select.Item key={o} value={o}>
                    {o}
                  </Select.Item>
                ))}
              </Select.Content>
            </Select.Root>
          ) : (
            <TextField.Root
              id={`f-${field.key}`}
              value={values[field.key] ?? ''}
              placeholder={field.placeholder}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => change(field.key, e.target.value)}
              aria-invalid={values[field.key] ? !!errors[field.key] : undefined}
            />
          )}
          {values[field.key] && errors[field.key] ? (
            <Text size="1" color="red" role="alert">
              {errors[field.key]}
            </Text>
          ) : field.help ? (
            <Text size="1" color="gray">
              {field.help}
            </Text>
          ) : null}
        </Flex>
      ))}

      <Flex direction="column" gap="1">
        <Text as="label" size="2" weight="medium" htmlFor="smoke-email">
          Try it as (optional)
        </Text>
        <TextField.Root
          id="smoke-email"
          value={smokeEmail}
          placeholder="you@studio.com"
          onChange={(e) => setSmokeEmail(e.target.value)}
        />
        <Text size="1" color="gray">
          DNA reads this person's projects, playlists and versions to prove the whole
          path works. Read-only.
        </Text>
      </Flex>

      <Flex gap="3" align="center">
        <Button onClick={() => void test()} disabled={!formValid || run.isPending}>
          {run.isPending ? <Spinner /> : 'Test connection'}
        </Button>
        <Button
          variant="solid"
          color="green"
          onClick={() => void save()}
          disabled={!isFresh || !canSave(report) || run.isPending || !!savedRevision}
          title={
            !isFresh && report
              ? 'The settings changed since the last test. Test again before saving.'
              : undefined
          }
        >
          Save configuration
        </Button>
      </Flex>

      {report && !isFresh && !savedRevision && (
        <Callout.Root color="amber" role="status">
          <Callout.Text>
            The settings changed since this result. Test again before saving.
          </Callout.Text>
        </Callout.Root>
      )}

      {error && (
        <Callout.Root color="red" role="alert">
          <Callout.Text>
            {error.message}
            {error.stale ? ' Reload the page to see the latest, then try again.' : ''}
          </Callout.Text>
          {error.issues.length > 0 && (
            <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
              {error.issues.map((issue, i) => (
                <li key={i}>
                  <Text size="1">
                    {issue.path || issue.section}: {issue.message}
                  </Text>
                </li>
              ))}
            </ul>
          )}
        </Callout.Root>
      )}

      {savedRevision && (
        <Callout.Root color="green" role="status">
          <Callout.Text>
            Saved as revision {savedRevision.revision}.{' '}
            {savedRevision.restart.length > 0
              ? `Restart the backend to apply: ${savedRevision.restart.join(', ')}. `
              : 'It is already in effect. '}
            Reload this page to see it in the app.
          </Callout.Text>
          <Flex mt="2">
            <Button size="1" variant="soft" onClick={() => window.location.reload()}>
              Reload now
            </Button>
          </Flex>
        </Callout.Root>
      )}

      {report && (
        <HandshakeReportView
          report={report}
          onAccept={isFresh && !savedRevision ? accept : undefined}
        />
      )}
    </Flex>
  );
}
