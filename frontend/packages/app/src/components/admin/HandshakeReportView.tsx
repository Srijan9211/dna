import { Badge, Button, Callout, Flex, Text } from '@radix-ui/themes';
import {
  AlertTriangle,
  CheckCircle2,
  MinusCircle,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import type {
  CheckStatus,
  HandshakeReport,
  HandshakeSuggestion,
} from '@dna/core';
import { applicableSuggestions, summarizeChecks } from './trackerSetupLogic';

const STATUS: Record<
  CheckStatus,
  { icon: LucideIcon; color: 'green' | 'amber' | 'red' | 'gray'; label: string }
> = {
  ok: { icon: CheckCircle2, color: 'green', label: 'Passed' },
  warn: { icon: AlertTriangle, color: 'amber', label: 'Warning' },
  fail: { icon: XCircle, color: 'red', label: 'Failed' },
  skip: { icon: MinusCircle, color: 'gray', label: 'Skipped' },
};

/** Facts a check reports (server version, the account used) worth showing inline. */
function detailLine(details: Record<string, unknown>): string | null {
  const facts = Object.entries(details)
    .filter(([, v]) => typeof v === 'string' || typeof v === 'number')
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`);
  return facts.length > 0 ? facts.join(' · ') : null;
}

interface Props {
  report: HandshakeReport;
  /** Accept a suggestion: the parent merges its override and re-runs the check. */
  onAccept?: (suggestion: HandshakeSuggestion) => void;
}

export function HandshakeReportView({ report, onAccept }: Props) {
  const summary = summarizeChecks(report);
  const fixes = applicableSuggestions(report);

  return (
    <Flex direction="column" gap="3" data-testid="handshake-report">
      <Callout.Root
        color={report.ok ? (summary.warn > 0 ? 'amber' : 'green') : 'red'}
        role="status"
      >
        <Callout.Text>
          {report.ok
            ? summary.warn > 0
              ? `Ready to save, with ${summary.warn} warning${summary.warn === 1 ? '' : 's'}.`
              : 'Everything checks out. Ready to save.'
            : `${summary.fail} check${summary.fail === 1 ? '' : 's'} failed. Fix ${summary.fail === 1 ? 'it' : 'them'} before saving.`}
        </Callout.Text>
      </Callout.Root>

      <Flex direction="column" gap="2" asChild>
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {report.checks.map((check) => {
            const { icon: Icon, color, label } = STATUS[check.status];
            const facts = detailLine(check.details);
            return (
              <li key={`${check.name}`} data-status={check.status}>
                <Flex gap="2" align="start">
                  <Icon
                    size={18}
                    aria-label={label}
                    style={{ color: `var(--${color}-9)`, marginTop: 2, flexShrink: 0 }}
                  />
                  <Flex direction="column" gap="1">
                    <Text size="2" weight="medium">
                      {check.name}
                    </Text>
                    <Text size="2" color={check.status === 'fail' ? 'red' : 'gray'}>
                      {check.message}
                    </Text>
                    {facts && (
                      <Text size="1" color="gray">
                        {facts}
                      </Text>
                    )}
                  </Flex>
                </Flex>
              </li>
            );
          })}
        </ul>
      </Flex>

      {report.suggestions.length > 0 && (
        <Flex direction="column" gap="2">
          <Text size="2" weight="bold">
            Suggested fixes
          </Text>
          {report.suggestions.map((s, i) => (
            <Flex
              key={`${s.path}-${i}`}
              direction="column"
              gap="1"
              data-testid="suggestion"
              style={{
                border: '1px solid var(--gray-6)',
                borderRadius: 'var(--radius-3)',
                padding: 'var(--space-3)',
              }}
            >
              <Text size="2">{s.problem}</Text>
              {s.options.length > 0 && (
                <Flex gap="1" align="center" wrap="wrap">
                  <Text size="1" color="gray">
                    Closest in the tracker:
                  </Text>
                  {s.options.map((option) => (
                    <Badge key={option} color="gray" variant="soft">
                      {option}
                    </Badge>
                  ))}
                </Flex>
              )}
              {s.override !== null && onAccept && (
                <Flex>
                  <Button size="1" variant="soft" onClick={() => onAccept(s)}>
                    Apply this fix and re-test
                  </Button>
                </Flex>
              )}
            </Flex>
          ))}
          {fixes.length > 1 && (
            <Text size="1" color="gray">
              Each fix is applied on its own; the test re-runs after each.
            </Text>
          )}
        </Flex>
      )}
    </Flex>
  );
}
