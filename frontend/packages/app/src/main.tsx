import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ThemeProvider } from 'styled-components';
import { Theme } from '@radix-ui/themes';
import { GoogleOAuthProvider } from '@react-oauth/google';
import App from './App';
import { darkTheme, lightTheme, GlobalStyles } from './styles';
import { EventProvider, ToastProvider, ThemeModeProvider, useThemeMode, AuthProvider } from './contexts';
import { HotkeysProvider } from './hotkeys';
import '@radix-ui/themes/styles.css';
import './index.css';

const queryClient = new QueryClient();
const googleClientId = import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined;

function ThemedApp() {
  const { mode } = useThemeMode();
  const activeTheme = mode === 'light' ? lightTheme : darkTheme;
  return (
    <ThemeProvider theme={activeTheme}>
      <Theme appearance={mode} accentColor="violet">
        <GlobalStyles />
        <AuthProvider>
          <HotkeysProvider>
            <ToastProvider>
              <EventProvider>
                <App />
              </EventProvider>
            </ToastProvider>
          </HotkeysProvider>
        </AuthProvider>
      </Theme>
    </ThemeProvider>
  );
}

const app = (
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeModeProvider>
        <ThemedApp />
      </ThemeModeProvider>
    </QueryClientProvider>
  </StrictMode>
);

createRoot(document.getElementById('root')!).render(
  googleClientId
    ? <GoogleOAuthProvider clientId={googleClientId}>{app}</GoogleOAuthProvider>
    : app
);
