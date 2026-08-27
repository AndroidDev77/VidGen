import type { JSX } from "react";

import { AppShell } from "../components/AppShell";
import { useAppContext } from "./apiContext";
import { AppRoutes } from "./router";

export function App(): JSX.Element {
  const { theme, toggleTheme, client } = useAppContext();
  return (
    <AppShell devUser={client.devUser} theme={theme} onToggleTheme={toggleTheme}>
      <AppRoutes />
    </AppShell>
  );
}
