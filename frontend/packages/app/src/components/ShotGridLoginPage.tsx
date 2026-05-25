import { useState } from 'react';
import styled from 'styled-components';
import { Button, Flex, Spinner, TextField } from '@radix-ui/themes';
import { useGoogleLogin } from '@react-oauth/google';
import { Logo } from './Logo';
import { useShotGridAuth } from '../contexts/ShotGridAuthContext';

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

const Divider = styled.div`
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 24px 0;

  &::before,
  &::after {
    content: '';
    flex: 1;
    border-top: 1px solid ${({ theme }) => theme.colors.border.subtle};
  }
`;

const DividerLabel = styled.span`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 12px;
  color: ${({ theme }) => theme.colors.text.muted};
  white-space: nowrap;
`;

const SectionHeading = styled.p`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: ${({ theme }) => theme.colors.text.muted};
  margin: 0 0 12px 0;
`;

const TabRow = styled.div`
  display: flex;
  gap: 8px;
  margin-bottom: 20px;
`;

const TabButton = styled.button<{ $active: boolean }>`
  flex: 1;
  padding: 8px 12px;
  border-radius: ${({ theme }) => theme.radii.md};
  border: 1px solid ${({ theme, $active }) =>
    $active ? theme.colors.accent.main : theme.colors.border.subtle};
  background: ${({ theme, $active }) =>
    $active ? theme.colors.accent.subtle : 'transparent'};
  color: ${({ theme, $active }) =>
    $active ? theme.colors.accent.main : theme.colors.text.secondary};
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 13px;
  font-weight: ${({ $active }) => ($active ? 600 : 400)};
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: ${({ theme }) => theme.colors.accent.main};
    color: ${({ theme }) => theme.colors.accent.main};
  }
`;

const GoogleButton = styled.button`
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 10px 16px;
  border-radius: ${({ theme }) => theme.radii.md};
  border: 1px solid ${({ theme }) => theme.colors.border.subtle};
  background: ${({ theme }) => theme.colors.bg.surface};
  color: ${({ theme }) => theme.colors.text.primary};
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    background: ${({ theme }) => theme.colors.bg.elevated};
    border-color: ${({ theme }) => theme.colors.border.default};
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
`;

const HelpSection = styled.details`
  font-family: ${({ theme }) => theme.fonts.sans};
  font-size: 13px;
  color: ${({ theme }) => theme.colors.text.muted};
  margin-top: 20px;

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

// ── Google logo SVG ────────────────────────────────────────────────────── //

function GoogleLogo() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
      <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z" fill="#4285F4"/>
      <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z" fill="#34A853"/>
      <path d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z" fill="#FBBC05"/>
      <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z" fill="#EA4335"/>
    </svg>
  );
}

// ── Autodesk logo SVG ──────────────────────────────────────────────────── //

function AutodeskLogo() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor">
      <path d="M12 2L2 19.5h20L12 2zm0 3.5l7.5 13H4.5L12 5.5z"/>
    </svg>
  );
}

// ── Types ──────────────────────────────────────────────────────────────── //

type ShotGridTab = 'sso' | 'pat';

// ── Google button (isolated so useGoogleLogin only runs inside GoogleOAuthProvider) ── //

interface GoogleSignInButtonProps {
  disabled: boolean;
  onSuccess: (accessToken: string) => void;
  onError: () => void;
}

function GoogleSignInButton({ disabled, onSuccess, onError }: GoogleSignInButtonProps) {
  const googleLogin = useGoogleLogin({
    onSuccess: (tokenResponse) => onSuccess(tokenResponse.access_token),
    onError,
  });

  return (
    <GoogleButton onClick={() => googleLogin()} disabled={disabled}>
      <GoogleLogo />
      Sign in with Google
    </GoogleButton>
  );
}

// ── Component ──────────────────────────────────────────────────────────── //

export function ShotGridLoginPage() {
  const {
    loginModes,
    modeWarning,
    ssoError,
    clearSsoError,
    isLoading,
    signIn,
    signInWithSso,
    signInWithGoogleToken,
  } = useShotGridAuth();

  // Which ShotGrid sub-method is active
  const defaultSgTab: ShotGridTab =
    loginModes.shotgrid_sso.enabled ? 'sso' : 'pat';
  const [sgTab, setSgTab] = useState<ShotGridTab>(defaultSgTab);

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // Determine which top-level sections are available
  const hasGoogle = loginModes.google.enabled;
  const hasShotGrid =
    loginModes.shotgrid_pat.enabled || loginModes.shotgrid_sso.enabled;

  // ── ShotGrid PAT submit ── //

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

  // ── Loading state ── //

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

        <Title>Sign in to DNA</Title>
        <Subtitle>Choose how you'd like to sign in</Subtitle>

        {/* ── Mode warning ── */}
        {modeWarning && (
          <div style={{
            padding: '10px 14px',
            marginBottom: 20,
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

        {/* ── SSO / backend error banner ── */}
        {ssoError && (
          <div style={{
            padding: '10px 14px',
            marginBottom: 20,
            borderRadius: 8,
            background: 'var(--red-2)',
            border: '1px solid var(--red-6)',
            fontSize: 13,
            color: 'var(--red-11)',
            lineHeight: 1.5,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: 8,
          }}>
            <span>⚠️ {ssoError}</span>
            <button
              onClick={clearSsoError}
              style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', fontSize: 16, lineHeight: 1, padding: 0 }}
              aria-label="Dismiss"
            >×</button>
          </div>
        )}

        {/* ── Google sign-in ── */}
        {hasGoogle && (
          <>
            <SectionHeading>Continue with</SectionHeading>
            <GoogleSignInButton
              disabled={submitting}
              onSuccess={async (accessToken) => {
                setError('');
                setSubmitting(true);
                try {
                  await signInWithGoogleToken(accessToken);
                } catch (err) {
                  setError(err instanceof Error ? err.message : 'Google sign-in failed.');
                } finally {
                  setSubmitting(false);
                }
              }}
              onError={() => setError('Google sign-in was cancelled or failed. Please try again.')}
            />
          </>
        )}

        {/* ── Divider ── */}
        {hasGoogle && hasShotGrid && (
          <Divider>
            <DividerLabel>or sign in with ShotGrid</DividerLabel>
          </Divider>
        )}

        {/* ── ShotGrid section ── */}
        {hasShotGrid && (
          <>
            {!hasGoogle && <SectionHeading>ShotGrid</SectionHeading>}

            {/* Show tab switcher only if both SG methods are enabled */}
            {loginModes.shotgrid_sso.enabled && loginModes.shotgrid_pat.enabled && (
              <TabRow>
                <TabButton
                  $active={sgTab === 'sso'}
                  onClick={() => { setSgTab('sso'); setError(''); }}
                >
                  Autodesk SSO
                </TabButton>
                <TabButton
                  $active={sgTab === 'pat'}
                  onClick={() => { setSgTab('pat'); setError(''); }}
                >
                  Password Login
                </TabButton>
              </TabRow>
            )}

            {/* ShotGrid SSO button */}
            {loginModes.shotgrid_sso.enabled && sgTab === 'sso' && (
              <Button
                size="3"
                variant="outline"
                style={{ width: '100%', gap: 8 }}
                onClick={() => {
                  setError('');
                  signInWithSso('shotgrid_sso');
                }}
                disabled={submitting}
              >
                <AutodeskLogo />
                Sign in with Autodesk
              </Button>
            )}

            {/* ShotGrid PAT form */}
            {loginModes.shotgrid_pat.enabled && sgTab === 'pat' && (
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
                    <FieldLabel htmlFor="sg-password">Password</FieldLabel>
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

                  <Button type="submit" size="3" disabled={submitting}>
                    {submitting ? <Spinner size="2" /> : 'Sign in'}
                  </Button>
                </Flex>
              </form>
            )}

            {/* PAT help */}
            {loginModes.shotgrid_pat.enabled && sgTab === 'pat' && (
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
            )}
          </>
        )}

        {/* ── Local error (PAT form or Google) ── */}
        {error && <ErrorText style={{ marginTop: 16 }}>{error}</ErrorText>}
      </Card>
    </PageWrapper>
  );
}
