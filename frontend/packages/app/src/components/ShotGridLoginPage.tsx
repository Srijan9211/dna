import { useState, type ReactNode } from 'react';
import styled from 'styled-components';
import { Button, Flex, Spinner, TextField } from '@radix-ui/themes';
import { Logo } from './Logo';
import {
  useShotGridAuth,
  type SessionAuthProviderType,
} from '../contexts/ShotGridAuthContext';
import { useRuntimeConfig } from '../contexts/RuntimeConfigContext';

// ── Styled components ──────────────────────────────────────────────────── //

const PageWrapper = styled.div`
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(
        ellipse 80% 50% at 50% -20%,
        ${({ theme }) => theme.colors.accent.subtle},
        transparent
      )
      fixed,
    ${({ theme }) => theme.colors.bg.base};
`;

const Card = styled.div`
  width: 100%;
  max-width: 440px;
  padding: 40px;
  background: ${({ theme }) => theme.colors.bg.elevated};
  border: 1px solid ${({ theme }) => theme.colors.border.subtle};
  border-radius: ${({ theme }) => theme.radii.xl};
  box-shadow: ${({ theme }) => theme.shadows.lg};
`;

const LogoWrapper = styled.div`
  display: flex;
  justify-content: center;
  margin-bottom: 32px;
`;

const Title = styled.h1`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 24px;
  font-weight: 600;
  color: ${({ theme }) => theme.colors.text.primary};
  text-align: center;
  margin: 0 0 8px 0;
`;

const Subtitle = styled.p`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 14px;
  color: ${({ theme }) => theme.colors.text.muted};
  text-align: center;
  margin: 0 0 28px 0;
`;

const FieldLabel = styled.label`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 14px;
  font-weight: 500;
  color: ${({ theme }) => theme.colors.text.secondary};
  display: block;
  margin-bottom: 6px;
`;

const ErrorText = styled.p`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 13px;
  color: ${({ theme }) => theme.colors.status.error};
  text-align: center;
  margin: 8px 0 0 0;
`;

const HelpText = styled.p`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 12px;
  color: ${({ theme }) => theme.colors.text.muted};
  margin: 16px 0 0 0;
  line-height: 1.5;
`;

// ── Per-tracker wording ────────────────────────────────────────────────── //

interface LoginCopy {
  subtitle: string;
  usernameLabel: string;
  usernamePlaceholder: string;
  passwordPlaceholder: string;
  help: ReactNode;
}

export function loginCopy(
  provider: SessionAuthProviderType,
  trackerName: string
): LoginCopy {
  if (provider === 'ayon') {
    return {
      subtitle: `Sign in with your ${trackerName} credentials`,
      usernameLabel: 'User name or email',
      usernamePlaceholder: 'you@studio.com',
      passwordPlaceholder: `${trackerName} password`,
      help: (
        <>
          Use your {trackerName} user name (or email) and password. DNA checks
          them with {trackerName} once and never stores your password.
        </>
      ),
    };
  }
  return {
    subtitle: `Sign in with your ${trackerName} credentials`,
    usernameLabel: 'Email / Username',
    usernamePlaceholder: 'you@studio.com',
    passwordPlaceholder: `${trackerName} Legacy Password`,
    help: (
      <>
        <strong>Cloud ShotGrid:</strong> Use your ShotGrid Legacy Login
        password. If you haven't set one, go to{' '}
        <em>Account Settings → Legacy Login and Personal Access Token</em> in
        ShotGrid and bind your Personal Access Token (generated at
        profile.autodesk.com).
        <br />
        <strong>On-prem ShotGrid:</strong> Use your regular ShotGrid or LDAP
        password.
      </>
    ),
  };
}

// ── Component ──────────────────────────────────────────────────────────── //

/** Username + password sign-in for whichever tracker the server is set up for. */
export function TrackerLoginPage() {
  const { signIn, isLoading, authProvider } = useShotGridAuth();
  const config = useRuntimeConfig();
  const copy = loginCopy(authProvider, config.tracker.displayName);

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await signIn(username, password);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Login failed. Please try again.'
      );
    }
  };

  return (
    <PageWrapper>
      <Card>
        <LogoWrapper>
          <Logo width={48} />
        </LogoWrapper>

        <Title>Welcome to DNA</Title>
        <Subtitle>{copy.subtitle}</Subtitle>

        <form onSubmit={handleSubmit}>
          <Flex direction="column" gap="3">
            <div>
              <FieldLabel htmlFor="username">{copy.usernameLabel}</FieldLabel>
              <TextField.Root
                id="username"
                type="text"
                placeholder={copy.usernamePlaceholder}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={isLoading}
                autoComplete="username"
                required
              />
            </div>

            <div>
              <FieldLabel htmlFor="password">Password</FieldLabel>
              <TextField.Root
                id="password"
                type="password"
                placeholder={copy.passwordPlaceholder}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={isLoading}
                autoComplete="current-password"
                required
              />
            </div>

            {error && <ErrorText>{error}</ErrorText>}

            <Button
              type="submit"
              disabled={isLoading || !username || !password}
              size="3"
            >
              {isLoading ? <Spinner /> : 'Sign in'}
            </Button>
          </Flex>
        </form>

        <HelpText>{copy.help}</HelpText>
      </Card>
    </PageWrapper>
  );
}

/** The original name, kept so existing imports keep working. */
export const ShotGridLoginPage = TrackerLoginPage;
