import { Button, Callout, Flex, Heading, Spinner, Tabs, Text } from '@radix-ui/themes';
import { ArrowLeft } from 'lucide-react';
import { useConfigAccess, useEffectiveConfig } from '../../api';
import { useRuntimeConfig } from '../../contexts';
import { HistoryPanel } from './HistoryPanel';
import { SettingsPanel } from './SettingsPanel';
import { TrackerSetup } from './TrackerSetup';

/** Leave the admin pages: drop `?admin=config` and return to the app. */
export function leaveAdmin(): void {
  const params = new URLSearchParams(window.location.search);
  params.delete('admin');
  const query = params.toString();
  window.location.assign(
    `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <Flex
      direction="column"
      gap="4"
      p="5"
      style={{ maxWidth: 880, margin: '0 auto', minHeight: '100vh' }}
    >
      {children}
    </Flex>
  );
}

/**
 * The configuration admin pages: set up the production tracker (with a real
 * connection test), see the effective settings and where they come from, and review
 * or roll back changes.  Offered only to configuration administrators.
 */
export function ConfigAdminPage() {
  const access = useConfigAccess();
  const { data: effective } = useEffectiveConfig(access.data?.admin === true);
  const runtime = useRuntimeConfig();

  if (access.isLoading) {
    return (
      <Frame>
        <Spinner />
      </Frame>
    );
  }

  if (!access.data?.admin) {
    return (
      <Frame>
        <Callout.Root color="gray" role="status">
          <Callout.Text>
            You do not have access to configuration. Ask an administrator, or return to
            DNA.
          </Callout.Text>
        </Callout.Root>
        <Flex>
          <Button variant="soft" onClick={leaveAdmin}>
            <ArrowLeft size={14} aria-hidden /> Back to DNA
          </Button>
        </Flex>
      </Frame>
    );
  }

  return (
    <Frame>
      <Flex justify="between" align="center">
        <Flex direction="column">
          <Heading size="6">Configuration</Heading>
          <Text size="2" color="gray">
            Connected to {runtime.tracker.displayName}
            {effective ? ` · revision ${effective.revision}` : ''}
          </Text>
        </Flex>
        <Button variant="soft" onClick={leaveAdmin}>
          <ArrowLeft size={14} aria-hidden /> Back to DNA
        </Button>
      </Flex>

      <Tabs.Root defaultValue="tracker">
        <Tabs.List>
          <Tabs.Trigger value="tracker">Tracker setup</Tabs.Trigger>
          <Tabs.Trigger value="settings">Settings</Tabs.Trigger>
          <Tabs.Trigger value="history">History</Tabs.Trigger>
        </Tabs.List>
        <Flex pt="4" direction="column">
          <Tabs.Content value="tracker">
            <TrackerSetup currentConnector={runtime.tracker.connector} />
          </Tabs.Content>
          <Tabs.Content value="settings">
            <SettingsPanel />
          </Tabs.Content>
          <Tabs.Content value="history">
            <HistoryPanel />
          </Tabs.Content>
        </Flex>
      </Tabs.Root>
    </Frame>
  );
}
