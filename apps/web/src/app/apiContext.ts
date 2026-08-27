import { createContext, useContext } from "react";

import { apiClient, type VidGenClient } from "../api/client";
import type { ThemePreference } from "./theme";

export interface AppContextValue {
  readonly theme: ThemePreference;
  readonly toggleTheme: () => void;
  readonly client: VidGenClient;
}

export const ApiClientContext = createContext<AppContextValue>({
  theme: "light",
  toggleTheme: () => undefined,
  client: apiClient,
});

export function useAppContext(): AppContextValue {
  return useContext(ApiClientContext);
}

export function useApiClient(): VidGenClient {
  return useAppContext().client;
}
