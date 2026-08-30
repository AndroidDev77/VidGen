import { Caption1, Subtitle2, makeStyles, mergeClasses, tokens } from "@fluentui/react-components";
import { useId, type JSX, type ReactNode } from "react";

/**
 * The layout primitives every page is built from.
 *
 * Panels across the app previously each invented their own padding, heading
 * level and border, so two cards side by side rarely lined up. Routing them
 * all through these three components is what makes the console read as one
 * product rather than as a set of separately built screens.
 */

const useStyles = makeStyles({
  card: {
    display: "flex",
    flexDirection: "column",
    minWidth: 0,
    borderRadius: tokens.borderRadiusLarge,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    boxShadow: tokens.shadow2,
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "flex-start",
    gap: tokens.spacingHorizontalM,
    padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalL}`,
    borderBottom: `1px solid ${tokens.colorNeutralStroke3}`,
  },
  // The icon sits in a tinted chip rather than loose in the text flow, which
  // keeps the heading baseline steady whatever glyph a panel passes in.
  icon: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    width: "32px",
    height: "32px",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground2,
    fontSize: tokens.fontSizeBase500,
  },
  headings: { display: "flex", flexDirection: "column", minWidth: 0, flexGrow: 1 },
  description: { color: tokens.colorNeutralForeground3 },
  headerActions: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
    flexShrink: 0,
  },
  body: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalM,
    padding: tokens.spacingHorizontalL,
    minWidth: 0,
  },
  bodyFlush: { padding: 0 },

  tile: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalXXS,
    minWidth: 0,
    padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalM}`,
    borderRadius: tokens.borderRadiusMedium,
    // Background1 rather than the page ground: a tile has to read as a raised
    // surface both on the page itself and nested inside a card.
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    // The tone shows as a left rule, so a warning tile is distinguishable in
    // greyscale and to a screen reader through its own text, never by colour.
    borderLeftWidth: "3px",
    borderLeftStyle: "solid",
    borderLeftColor: tokens.colorNeutralStroke1,
  },
  tileNeutral: { borderLeftColor: tokens.colorNeutralStroke1 },
  tileBrand: { borderLeftColor: tokens.colorBrandStroke1 },
  tileSuccess: { borderLeftColor: tokens.colorPaletteGreenBorderActive },
  tileWarning: { borderLeftColor: tokens.colorPaletteYellowBorderActive },
  tileDanger: { borderLeftColor: tokens.colorPaletteRedBorderActive },
  tileLabel: {
    color: tokens.colorNeutralForeground3,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    fontWeight: tokens.fontWeightSemibold,
  },
  tileValue: {
    fontSize: tokens.fontSizeBase500,
    lineHeight: tokens.lineHeightBase500,
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    fontVariantNumeric: "tabular-nums",
    overflowWrap: "anywhere",
  },
  tileHint: { color: tokens.colorNeutralForeground3 },

  stack: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    minWidth: 0,
  },
  tiles: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
    gap: tokens.spacingHorizontalS,
  },
});

export type Tone = "neutral" | "brand" | "success" | "warning" | "danger";

export interface SectionCardProps {
  readonly title?: string;
  /**
   * An explicit accessible name for the landmark, when it should differ from
   * the visible title (a panel titled "Publish to YouTube" that the rest of
   * the app refers to as the YouTube publication region, for instance).
   */
  readonly ariaLabel?: string;
  readonly description?: ReactNode;
  readonly icon?: ReactNode;
  readonly actions?: ReactNode;
  /** Heading level for the card title. Defaults to `h2`. */
  readonly headingAs?: "h2" | "h3";
  /** Drop the body padding for content that manages its own edges, e.g. a table. */
  readonly flush?: boolean;
  readonly className?: string;
  readonly children?: ReactNode;
}

export function SectionCard({
  title,
  ariaLabel,
  description,
  icon,
  actions,
  headingAs = "h2",
  flush = false,
  className,
  children,
}: SectionCardProps): JSX.Element {
  const styles = useStyles();
  const titleId = useId();
  const hasHeader = title !== undefined || actions !== undefined;
  const body = (
    <>
      {hasHeader && (
        <div className={styles.header}>
          {icon !== undefined && (
            <span className={styles.icon} aria-hidden="true">
              {icon}
            </span>
          )}
          <div className={styles.headings}>
            {title !== undefined && (
              <Subtitle2 as={headingAs} id={titleId}>
                {title}
              </Subtitle2>
            )}
            {description !== undefined && (
              <Caption1 className={styles.description}>{description}</Caption1>
            )}
          </div>
          {actions !== undefined && <div className={styles.headerActions}>{actions}</div>}
        </div>
      )}
      <div className={mergeClasses(styles.body, flush && styles.bodyFlush)}>{children}</div>
    </>
  );

  // A titled card is a real landmark, named by its own heading, so assistive
  // technology can jump between the panels of a dashboard. An untitled one is
  // only chrome and stays a plain div rather than an unnamed region.
  if (title === undefined && ariaLabel === undefined) {
    return <div className={mergeClasses(styles.card, className)}>{body}</div>;
  }
  return (
    <section
      className={mergeClasses(styles.card, className)}
      {...(ariaLabel === undefined
        ? { "aria-labelledby": titleId }
        : { "aria-label": ariaLabel })}
    >
      {body}
    </section>
  );
}

export interface StatTileProps {
  readonly label: string;
  readonly value: ReactNode;
  readonly hint?: ReactNode;
  readonly tone?: Tone;
}

/** One headline number with the words that make it mean something. */
export function StatTile({ label, value, hint, tone = "neutral" }: StatTileProps): JSX.Element {
  const styles = useStyles();
  const toneClass = {
    neutral: styles.tileNeutral,
    brand: styles.tileBrand,
    success: styles.tileSuccess,
    warning: styles.tileWarning,
    danger: styles.tileDanger,
  }[tone];
  return (
    <div className={mergeClasses(styles.tile, toneClass)}>
      <Caption1 className={styles.tileLabel}>{label}</Caption1>
      <span className={styles.tileValue}>{value}</span>
      {hint !== undefined && <Caption1 className={styles.tileHint}>{hint}</Caption1>}
    </div>
  );
}

/**
 * The vertical rhythm of a page.
 *
 * Every route stacks its header, banners and panels through this, so the gap
 * between two sections is the same on every screen instead of depending on
 * whichever component happened to carry a margin.
 */
export function PageStack({
  children,
  className,
}: {
  readonly children: ReactNode;
  readonly className?: string;
}): JSX.Element {
  const styles = useStyles();
  return <div className={mergeClasses(styles.stack, className)}>{children}</div>;
}

/** A responsive row of {@link StatTile}s that reflows to one column on a phone. */
export function StatTiles({ children }: { readonly children: ReactNode }): JSX.Element {
  const styles = useStyles();
  return <div className={styles.tiles}>{children}</div>;
}
