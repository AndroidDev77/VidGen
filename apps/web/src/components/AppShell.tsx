import {
  Avatar,
  Body1,
  Button,
  Caption1,
  Link as FluentLink,
  Tooltip,
  makeStyles,
  mergeClasses,
  shorthands,
  tokens,
} from "@fluentui/react-components";
import { DarkThemeRegular, WeatherSunnyRegular } from "@fluentui/react-icons";
import type { JSX, ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import type { ThemePreference } from "../app/theme";

const useStyles = makeStyles({
  shell: {
    display: "flex",
    flexDirection: "column",
    // Viewport units, not a percentage: FluentProvider inserts its own element
    // between #root and here, which breaks a `height: 100%` chain and leaves
    // the document ground showing under a short page.
    minHeight: "100vh",
    backgroundColor: tokens.colorNeutralBackground2,
    // A shallow brand band behind the header only. Washing the whole page in
    // brand tint drowns the cards; 360px is enough to warm the masthead and
    // then hand the page back to the neutral ground.
    backgroundImage: `linear-gradient(to bottom, ${tokens.colorBrandBackground2}, transparent)`,
    backgroundSize: "100% 360px",
    backgroundRepeat: "no-repeat",
  },
  header: {
    position: "sticky",
    top: 0,
    zIndex: 100,
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: tokens.spacingHorizontalL,
    padding: `${tokens.spacingVerticalS} ${tokens.spacingHorizontalXL}`,
    borderBottom: `1px solid ${tokens.colorNeutralStroke2}`,
    // The header is translucent so the page shows through as it scrolls; where
    // backdrop-filter is unsupported the colour-mix falls back to a solid
    // surface rather than to unreadable transparency.
    backgroundColor: tokens.colorNeutralBackground1,
    flexWrap: "wrap",
    "@supports (backdrop-filter: blur(1px))": {
      backdropFilter: "saturate(180%) blur(16px)",
      backgroundColor: `color-mix(in srgb, ${tokens.colorNeutralBackground1} 82%, transparent)`,
    },
  },
  headerInner: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: tokens.spacingHorizontalL,
    width: "100%",
    maxWidth: "1440px",
    margin: "0 auto",
    flexWrap: "wrap",
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
    textDecoration: "none",
    color: tokens.colorNeutralForeground1,
    ...shorthands.borderRadius(tokens.borderRadiusMedium),
  },
  // The mark is drawn rather than iconised: a gradient tile with a play glyph
  // reads as a product logo at 32px, where a stock outline icon reads as a
  // toolbar button.
  mark: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: "32px",
    height: "32px",
    flexShrink: 0,
    borderRadius: tokens.borderRadiusMedium,
    color: tokens.colorNeutralForegroundOnBrand,
    backgroundImage: `linear-gradient(135deg, ${tokens.colorBrandBackground} 0%, ${tokens.colorBrandBackgroundHover} 55%, ${tokens.colorPaletteBerryBackground3} 100%)`,
    boxShadow: tokens.shadow4,
  },
  wordmark: {
    display: "flex",
    flexDirection: "column",
    lineHeight: tokens.lineHeightBase200,
  },
  // A span, not a heading: each page owns the document's single h1, and the
  // wordmark must not compete with it in the outline.
  wordmarkTitle: {
    fontSize: tokens.fontSizeBase400,
    fontWeight: tokens.fontWeightSemibold,
    letterSpacing: "-0.01em",
    margin: 0,
  },
  wordmarkSub: {
    color: tokens.colorNeutralForeground3,
    fontSize: tokens.fontSizeBase100,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
  },
  nav: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalXXS,
    marginLeft: tokens.spacingHorizontalL,
  },
  navLink: {
    display: "inline-flex",
    alignItems: "center",
    padding: `${tokens.spacingVerticalXS} ${tokens.spacingHorizontalM}`,
    borderRadius: tokens.borderRadiusMedium,
    textDecoration: "none",
    color: tokens.colorNeutralForeground2,
    fontSize: tokens.fontSizeBase300,
    fontWeight: tokens.fontWeightMedium,
    ":hover": {
      backgroundColor: tokens.colorNeutralBackground1Hover,
      color: tokens.colorNeutralForeground1,
    },
  },
  navLinkActive: {
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground2,
    fontWeight: tokens.fontWeightSemibold,
  },
  identity: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
  },
  identityText: {
    display: "flex",
    flexDirection: "column",
    lineHeight: tokens.lineHeightBase200,
  },
  identityName: { fontWeight: tokens.fontWeightSemibold },
  identityRole: { color: tokens.colorNeutralForeground3 },
  // The identity block is the first thing to go when the viewport narrows; the
  // avatar keeps the account visible on its own.
  identityDetail: {
    "@media (max-width: 640px)": { display: "none" },
  },
  main: {
    flexGrow: 1,
    width: "100%",
    maxWidth: "1440px",
    margin: "0 auto",
    padding: `${tokens.spacingVerticalXXL} ${tokens.spacingHorizontalXL}`,
    "@media (max-width: 640px)": {
      padding: `${tokens.spacingVerticalL} ${tokens.spacingHorizontalM}`,
    },
  },
  footer: {
    width: "100%",
    maxWidth: "1440px",
    margin: "0 auto",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: tokens.spacingHorizontalM,
    flexWrap: "wrap",
    padding: `${tokens.spacingVerticalL} ${tokens.spacingHorizontalXL}`,
    borderTop: `1px solid ${tokens.colorNeutralStroke3}`,
    color: tokens.colorNeutralForeground3,
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
  const location = useLocation();
  const onProjects = location.pathname.startsWith("/projects");
  const nextTheme = theme === "dark" ? "Light theme" : "Dark theme";

  return (
    <div className={styles.shell}>
      <FluentLink className="vidgen-skip-link" href="#main-content">
        Skip to main content
      </FluentLink>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <div className={styles.identity}>
            <Link className={styles.brand} to="/projects" aria-label="VidGen Studio home">
              <span className={styles.mark} aria-hidden="true">
                <PlayMark />
              </span>
              <span className={styles.wordmark}>
                <span className={styles.wordmarkTitle}>VidGen Studio</span>
                <span className={styles.wordmarkSub}>Recap pipeline</span>
              </span>
            </Link>
            <nav className={styles.nav} aria-label="Primary">
              <Link
                to="/projects"
                className={mergeClasses(styles.navLink, onProjects && styles.navLinkActive)}
                aria-current={onProjects ? "page" : undefined}
              >
                Projects
              </Link>
            </nav>
          </div>
          <div className={styles.identity}>
            <Tooltip
              content="Local development identity sent as X-VidGen-User. Not production authentication."
              relationship="description"
            >
              <div className={styles.identity}>
                <Avatar name={devUser} size={32} color="colorful" aria-hidden="true" />
                <span
                  className={mergeClasses(styles.identityText, styles.identityDetail)}
                  data-testid="app-shell-identity"
                >
                  <Body1 className={styles.identityName}>{devUser}</Body1>
                  <Caption1 className={styles.identityRole}>Signed in</Caption1>
                </span>
              </div>
            </Tooltip>
            <Tooltip content={nextTheme} relationship="label">
              <Button
                appearance="subtle"
                shape="circular"
                aria-label={nextTheme}
                icon={theme === "dark" ? <WeatherSunnyRegular /> : <DarkThemeRegular />}
                onClick={onToggleTheme}
              />
            </Tooltip>
          </div>
        </div>
      </header>
      <main id="main-content" className={styles.main} tabIndex={-1}>
        {children}
      </main>
      <footer className={styles.footer}>
        <Caption1>VidGen Studio — restartable media workflow console.</Caption1>
        <Caption1>All times shown in UTC.</Caption1>
      </footer>
    </div>
  );
}

/** The wordmark glyph: a play triangle inside the brand tile. */
function PlayMark(): JSX.Element {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" focusable="false" aria-hidden="true">
      <path d="M5.2 3.4v9.2a.6.6 0 0 0 .92.5l7.1-4.6a.6.6 0 0 0 0-1l-7.1-4.6a.6.6 0 0 0-.92.5Z" fill="currentColor" />
    </svg>
  );
}
