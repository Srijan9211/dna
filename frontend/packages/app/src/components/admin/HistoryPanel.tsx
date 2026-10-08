import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AlertDialog,
  Badge,
  Button,
  Callout,
  Flex,
  Spinner,
  Table,
  Text,
} from '@radix-ui/themes';
import { apiHandler, useConfigHistory } from '../../api';
import { describeAdminError, type AdminError } from './adminErrors';
import { showValue } from './projectOverrides';

function when(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

/** Every recorded change, newest first, with who made it and a way back. */
export function HistoryPanel() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useConfigHistory();
  const [failure, setFailure] = useState<AdminError | null>(null);
  const [done, setDone] = useState<number | null>(null);

  const rollback = useMutation({
    mutationFn: (version: number) => apiHandler.rollbackConfig(version),
    onSuccess: async (result) => {
      setFailure(null);
      setDone(result.revision);
      await queryClient.invalidateQueries({ queryKey: ['config'] });
    },
    onError: (err) => setFailure(describeAdminError(err)),
  });

  if (isLoading) return <Spinner />;
  if (error || !data) {
    return (
      <Callout.Root color="red" role="alert">
        <Callout.Text>Could not load the history.</Callout.Text>
      </Callout.Root>
    );
  }
  if (data.length === 0) {
    return (
      <Text size="2" color="gray">
        No changes have been saved yet. Every change you save here is recorded
        with your name and can be undone.
      </Text>
    );
  }

  const latest = data[0].version;

  return (
    <Flex direction="column" gap="3">
      {failure && (
        <Callout.Root color="red" role="alert">
          <Callout.Text>{failure.message}</Callout.Text>
        </Callout.Root>
      )}
      {done !== null && (
        <Callout.Root color="green" role="status">
          <Callout.Text>Rolled back. Saved as revision {done}.</Callout.Text>
        </Callout.Root>
      )}
      <Table.Root size="1">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeaderCell>Revision</Table.ColumnHeaderCell>
            <Table.ColumnHeaderCell>When</Table.ColumnHeaderCell>
            <Table.ColumnHeaderCell>Who</Table.ColumnHeaderCell>
            <Table.ColumnHeaderCell>What</Table.ColumnHeaderCell>
            <Table.ColumnHeaderCell />
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {data.map((rev) => (
            <Table.Row key={rev.version}>
              <Table.RowHeaderCell>
                v{rev.version}{' '}
                {rev.version === latest && <Badge color="green">current</Badge>}
              </Table.RowHeaderCell>
              <Table.Cell>{when(rev.at)}</Table.Cell>
              <Table.Cell>{rev.actor}</Table.Cell>
              <Table.Cell>
                <Flex direction="column">
                  {rev.note && <Text size="2">{rev.note}</Text>}
                  {rev.details && rev.details.length > 0 ? (
                    rev.details.map((d) => (
                      <Text key={d.path} size="1" color="gray">
                        {`${d.path}: ${showValue(d.before)} → ${showValue(d.after)}`}
                      </Text>
                    ))
                  ) : (
                    <Text size="1" color="gray">
                      {rev.changed.length > 0
                        ? rev.changed.join(', ')
                        : 'no setting changed'}
                    </Text>
                  )}
                </Flex>
              </Table.Cell>
              <Table.Cell>
                {rev.version !== latest && (
                  <AlertDialog.Root>
                    <AlertDialog.Trigger>
                      <Button
                        size="1"
                        variant="soft"
                        color="gray"
                        aria-label={`Roll back to revision ${rev.version}`}
                      >
                        Roll back to this
                      </Button>
                    </AlertDialog.Trigger>
                    <AlertDialog.Content maxWidth="440px">
                      <AlertDialog.Title>
                        Roll back to revision {rev.version}?
                      </AlertDialog.Title>
                      <AlertDialog.Description size="2">
                        The configuration returns to how it was then. This is
                        saved as a new revision, so nothing is lost and you can
                        undo it again.
                      </AlertDialog.Description>
                      <Flex gap="3" mt="4" justify="end">
                        <AlertDialog.Cancel>
                          <Button variant="soft" color="gray">
                            Cancel
                          </Button>
                        </AlertDialog.Cancel>
                        <AlertDialog.Action>
                          <Button onClick={() => rollback.mutate(rev.version)}>
                            Roll back
                          </Button>
                        </AlertDialog.Action>
                      </Flex>
                    </AlertDialog.Content>
                  </AlertDialog.Root>
                )}
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
    </Flex>
  );
}
