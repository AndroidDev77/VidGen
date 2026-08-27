import { QueryClient } from "@tanstack/react-query";
import { render as rtlRender, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { VidGenClient } from "../api/client";
import { AppProviders } from "../app/providers";

export const testClient = new VidGenClient({
  apiBaseUrl: "http://localhost",
  devUser: "owner-a",
  isDevelopment: false,
});

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

export interface RenderOptions {
  readonly route?: string;
  readonly queryClient?: QueryClient;
}

/** Render a component inside the app's providers and a memory router. */
export function renderWithProviders(
  ui: ReactElement,
  options: RenderOptions = {},
): RenderResult & { readonly queryClient: QueryClient } {
  const queryClient = options.queryClient ?? createTestQueryClient();
  const result = rtlRender(
    <MemoryRouter initialEntries={[options.route ?? "/"]}>
      <AppProviders queryClient={queryClient} client={testClient}>
        {ui}
      </AppProviders>
    </MemoryRouter>,
  );
  return Object.assign(result, { queryClient });
}
