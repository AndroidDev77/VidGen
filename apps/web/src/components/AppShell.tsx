import {
  Body1,
  Button,
  Divider,
  Link as FluentLink,
  Title3,
  Tooltip,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { DarkThemeRegular, VideoClipMultipleRegular, WeatherSunnyRegular } from "@fluentui/react-icons";
import type { JSX, ReactNode } from "react";
import { Link } from "react-router-dom";

import type { ThemePreference } from "../app/theme";

const useStyles = makeStyles({
  shell: {
    display: "flex",
    flexDirection: "column",
    minHeight: "100%",
    backgroundColor: tokens.colorNeutralBackground2,
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: tokens.spacingHorizontalL,
    padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalXL}`,
    backgroundColor: tokens.colorNeutralBackground1,
    flexWrap: "wrap",
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
    textDecoration: "none",
    color: tokens.colorNeutralForeground1,
  },
  main: {
    flexGrow: 1,
    width: "100%",
    maxWidth: "1440px",
    margin: "0 auto",
    padding: `${tokens.spacingVerticalXL} ${tokens.spacingHorizontalXL}`,
  },
  identity: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalM,
  },
});

export interface AppShellProps {
  readonly children: ReactNode;
  readonly devUser: string;
  readonly theme: ThemePreference;
  readonly onToggleTheme: () => void;
}

export function AppShell({ children, devUser, theme, onToggleTheme }: AppShellProps): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.shell}>
      <FluentLink className="vidgen-skip-link" href="#main-content">
        Skip to main content
      </FluentLink>
      <header className={styles.header}>
        <Link className={styles.brand} to="/projects">
          <VideoClipMultipleRegular fontSize={28} aria-hidden="true" />
          <Title3 as="h1">VidGen Studio</Title3>
        </Link>
        <div className={styles.identity}>
          <Tooltip
            content="Local development identity sent as X-VidGen-User. Not production authentication."
            relationship="description"
          >
            <Body1>Signed in as {devUser}</Body1>
          </Tooltip>
          <Button
            appearance="subtle"
            icon={theme === "dark" ? <WeatherSunnyRegular /> : <DarkThemeRegular />}
            onClick={onToggleTheme}
          >
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </Button>
        </div>
      </header>
      <Divider />
      <main id="main-content" className={styles.main} tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}
