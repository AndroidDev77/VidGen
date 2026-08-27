import { FluentProvider } from "@fluentui/react-components";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCallback, useMemo, useState, type JSX, type ReactNode } from "react";

import { apiClient, type VidGenClient } from "../api/client";
import { VidGenApiError } from "../api/errors";
import { ApiClientContext } from "./apiContext";
import { THEME_STORAGE_KEY, vidgenDarkTheme, vidgenLightTheme, type ThemePreference } from "./theme";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // Ownership and validation failures never become true on a retry.
          if (error instanceof VidGenApiError && !error.retryable) {
            return false;
          }
          return failureCount < 2;
        },
      },
      // Mutations carry an Idempotency-Key, but an automatic retry would hide
      // real failures from the user, so it stays opt-in.
      mutations: { retry: false },
    },
  });
}

function readStoredTheme(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "dark" || stored === "light") {
      return stored;
    }
  } catch {
    // Private windows and blocked site data are normal; fall through.
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export interface AppProvidersProps {
  readonly children: ReactNode;
  readonly queryClient?: QueryClient;
  readonly client?: VidGenClient;
}

export function AppProviders({ children, queryClient, client }: AppProvidersProps): JSX.Element {
  const [resolvedClient] = useState(() => queryClient ?? createQueryClient());
  const [theme, setTheme] = useState<ThemePreference>(readStoredTheme);

  const toggleTheme = useCallback(() => {
    setTheme((previous) => {
      const next: ThemePreference = previous === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch {
        // A per-viewer convenience only; failure to persist is not an error.
      }
      return next;
    });
  }, []);

  const themeValue = useMemo(
    () => ({ theme, toggleTheme, client: client ?? apiClient }),
    [theme, toggleTheme, client],
  );

  return (
    <QueryClientProvider client={resolvedClient}>
      <ApiClientContext.Provider value={themeValue}>
        <FluentProvider theme={theme === "dark" ? vidgenDarkTheme : vidgenLightTheme}>
          {children}
        </FluentProvider>
      </ApiClientContext.Provider>
    </QueryClientProvider>
  );
}
