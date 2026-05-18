import { useState } from 'react';
import styled from 'styled-components';
import { Button, Flex, Spinner, TextField } from '@radix-ui/themes';
import { Logo } from './Logo';
import { useShotGridAuth, type ShotGridAuthMode } from '../contexts/ShotGridAuthContext';

// ── Styled components (mirrors ProjectSelector card style) ─────────────── //

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
  max-width: 420px;
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
  margin: 0 0 32px 0;
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

const Divider = styled.hr`
  border: none;
  border-top: 1px solid ${({ theme }) => theme.colors.border.subtle};
  margin: 24px 0;
`;

const HelpSection = styled.details`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 13px;
  color: ${({ theme }) => theme.colors.text.muted};

  summary {
    cursor: pointer;
    font-weight: 500;
    color: ${({ theme }) => theme.colors.text.secondary};
    list-style: none;
    display: flex;
    align-items: center;
    gap: 6px;

    &::before {
      content: '▶';
      font-size: 10px;
      transition: transform 0.15s;
    }
  }

  &[open] summary::before {
    transform: rotate(90deg);
  }
`;

const HelpContent = styled.div`
  margin-top: 12px;
  padding: 16px;
  background: ${({ theme }) => theme.colors.bg.surface};
  border-radius: ${({ theme }) => theme.radii.md};
  line-height: 1.6;

  ol {
    margin: 8px 0 0 0;
    padding-left: 20px;
  }

  li {
    margin-bottom: 6px;
  }

  a {
    color: ${({ theme }) => theme.colors.accent.main};
    text-decoration: none;

    &:hover {
      text-decoration: underline;
    }
  }
`;

const ModeBadge = styled.span<{ $mode: ShotGridAuthMode }>`
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  background: ${({ theme, $mode }) =>
    $mode === 'sso' ? theme.colors.accent.subtle : theme.colors.bg.surface};
  color: ${({ theme, $mode }) =>
    $mode === 'sso' ? theme.colors.accent.main : theme.colors.text.muted};
  border: 1px solid ${({ theme, $mode }) =>
    $mode === 'sso' ? theme.colors.accent.main : theme.colors.border.subtle};
`;

// ── Component ─────────────────────────────────────────────────────────── //

export function ShotGridLoginPage() {
  const { mode, modeWarning, isLoading, signIn } = useShotGridAuth();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handlePatSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password.trim()) {
      setError('Please enter your email and password.');
      return;
    }
    setError('');
    setSubmitting(true);
    try {
      await signIn(username.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed. Check your credentials.');
    } finally {
      setSubmitting(false);
    }
  };

  const handleSsoRedirect = () => {
    // Fetch a fresh redirect_url (generates a new CSRF state stored in Redis)
    // then open the ShotGrid/Autodesk authorization page in a POPUP window.
    //
    // Why popup instead of same-tab redirect?
    //   • User stays on DNA — no page abandonment, no loading flicker.
    //   • After authentication the popup navigates to /auth/callback, the
    //     React app running there detects window.opener and sends the auth
    //     params back via window.postMessage, then closes itself.
    //   • The parent DNA tab receives the postMessage event (listened for in
    //     ShotGridAuthContext) and completes the login seamlessly.
    const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

    fetch(`${apiBase}/auth/login`)
      .then((r) => r.json())
      .then((data) => {
        if (!data.redirect_url) {
          setError('No redirect URL returned from server. Check SHOTGRID_CLIENT_ID.');
          return;
        }
        // Popup dimensions — centred on screen
        const width = 560;
        const height = 680;
        const left = Math.round(window.screenX + (window.outerWidth - width) / 2);
        const top = Math.round(window.screenY + (window.outerHeight - height) / 2);
        const features = `width=${width},height=${height},left=${left},top=${top},scrollbars=yes,resizable=yes`;

        const popup = window.open(data.redirect_url, 'dna_sso_login', features);

        if (!popup || popup.closed) {
          // Browser blocked the popup — fall back to same-tab redirect
          setError('Popup was blocked. Please allow popups for this site, then try again.');
          return;
        }

        // Focus the popup
        popup.focus();

        // Poll to detect if the user closed the popup without completing auth
        const pollTimer = setInterval(() => {
          if (popup.closed) {
            clearInterval(pollTimer);
          }
        }, 500);
      })
      .catch(() => setError('Failed to initiate ShotGrid login. Please try again.'));
  };

  if (isLoading) {
    return (
      <PageWrapper>
        <Flex align="center" justify="center" gap="3" style={{ color: 'var(--gray-11)' }}>
          <Spinner size="3" />
          <span>Connecting…</span>
        </Flex>
      </PageWrapper>
    );
  }

  return (
    <PageWrapper>
      <Card>
        <LogoWrapper>
          <Logo />
        </LogoWrapper>

        <Flex align="center" justify="center" gap="2" style={{ marginBottom: 8 }}>
          <Title style={{ margin: 0 }}>Sign in to DNA</Title>
          {mode && <ModeBadge $mode={mode}>{mode}</ModeBadge>}
        </Flex>

        <Subtitle>
          {mode === 'sso'
            ? 'Sign in with your ShotGrid account'
            : 'Enter your ShotGrid credentials'}
        </Subtitle>

        {/* ── SSO-to-PAT fallback warning ── */}
        {modeWarning && (
          <div style={{
            padding: '10px 14px',
            marginBottom: 16,
            borderRadius: 8,
            background: 'var(--amber-2)',
            border: '1px solid var(--amber-6)',
            fontSize: 13,
            color: 'var(--amber-11)',
            lineHeight: 1.5,
          }}>
            ⚠️ {modeWarning}
          </div>
        )}

        {/* ── SSO mode ── */}
        {mode === 'sso' && (
          <Button
            size="3"
            style={{ width: '100%' }}
            onClick={handleSsoRedirect}
          >
            Sign in with ShotGrid
          </Button>
        )}

        {/* ── PAT mode ── */}
        {mode === 'pat' && (
          <form onSubmit={handlePatSubmit}>
            <Flex direction="column" gap="4">
              <div>
                <FieldLabel htmlFor="sg-username">Email</FieldLabel>
                <TextField.Root
                  id="sg-username"
                  type="email"
                  placeholder="you@studio.com"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  disabled={submitting}
                  size="3"
                />
              </div>

              <div>
                <FieldLabel htmlFor="sg-password">Legacy Login Password</FieldLabel>
                <TextField.Root
                  id="sg-password"
                  type="password"
                  placeholder="Your ShotGrid legacy password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  disabled={submitting}
                  size="3"
                />
              </div>

              {error && <ErrorText>{error}</ErrorText>}

              <Button type="submit" size="3" disabled={submitting}>
                {submitting ? <Spinner size="2" /> : 'Sign in'}
              </Button>
            </Flex>
          </form>
        )}

        {/* ── PAT setup help ── */}
        {mode === 'pat' && (
          <>
            <Divider />
            <HelpSection>
              <summary>First time? Set up your password</summary>
              <HelpContent>
                <strong>Cloud ShotGrid (one-time setup per user):</strong>
                <ol>
                  <li>
                    Go to{' '}
                    <a
                      href="https://profile.autodesk.com/security"
                      target="_blank"
                      rel="noreferrer"
                    >
                      profile.autodesk.com → Security
                    </a>{' '}
                    → <em>Personal Access Tokens</em> → Generate a token with
                    scope <strong>Flow Production Tracking</strong>. Copy it.
                  </li>
                  <li>
                    In ShotGrid → <em>Account Settings</em> →{' '}
                    <em>Legacy Login and Personal Access Token</em> → paste the
                    token and set a password. That password is what you enter
                    above.
                  </li>
                </ol>
                <p style={{ marginTop: 8, marginBottom: 0 }}>
                  <strong>On-prem ShotGrid:</strong> Use your actual ShotGrid or
                  LDAP password — no PAT needed.
                </p>
              </HelpContent>
            </HelpSection>
          </>
        )}

        {/* ── Error for SSO mode ── */}
        {mode === 'sso' && error && <ErrorText style={{ marginTop: 16 }}>{error}</ErrorText>}
      </Card>
    </PageWrapper>
  );
}
