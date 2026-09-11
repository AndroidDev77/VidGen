import {
  Body1,
  Breadcrumb,
  BreadcrumbButton,
  BreadcrumbDivider,
  BreadcrumbItem,
  Caption1,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components";
import {
  ClockRegular,
  FlowRegular,
  VideoClipMultipleRegular,
} from "@fluentui/react-icons";
import type { JSX, ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import type { WorkflowStatusProjection } from "@vidgen/contracts";

import { formatDurationSeconds, formatStage } from "../state/format";
import { StatusBadge } from "./StatusBadge";

const useStyles = makeStyles({
  header: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    marginBottom: tokens.spacingVerticalXL,
  },
  row: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalM,
    flexWrap: "wrap",
  },
  // A plain react-router Link would fall back to the browser's default link
  // colour, which is unreadable on the dark surface.
  crumbLink: {
    color: tokens.colorNeutralForeground2,
    textDecoration: "none",
    ":hover": { color: tokens.colorBrandForegroundLink, textDecoration: "underline" },
  },
  title: {
    fontSize: tokens.fontSizeHero700,
    lineHeight: tokens.lineHeightHero700,
    fontWeight: tokens.fontWeightSemibold,
    letterSpacing: "-0.02em",
    margin: 0,
  },
  spacer: { flexGrow: 1 },

  // A row of small labelled facts. Icons carry no meaning on their own; every
  // chip states its value in words next to the glyph.
  meta: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalL,
    flexWrap: "wrap",
    color: tokens.colorNeutralForeground3,
  },
  metaItem: {
    display: "inline-flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalXS,
  },
  metaValue: { color: tokens.colorNeutralForeground1, fontVariantNumeric: "tabular-nums" },

  live: {
    display: "inline-flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalXS,
    padding: `2px ${tokens.spacingHorizontalS}`,
    borderRadius: tokens.borderRadiusCircular,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  liveDot: {
    width: "8px",
    height: "8px",
    borderRadius: tokens.borderRadiusCircular,
    backgroundColor: tokens.colorNeutralForeground4,
    flexShrink: 0,
  },
  liveDotOn: { backgroundColor: tokens.colorPaletteGreenBorderActive },
  liveDotWaiting: { backgroundColor: tokens.colorPaletteYellowBorderActive },

  nav: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalXXS,
    marginTop: tokens.spacingVerticalXS,
    paddingBottom: tokens.spacingVerticalXS,
    borderBottom: `1px solid ${tokens.colorNeutralStroke2}`,
    overflowX: "auto",
  },
  navLink: {
    display: "inline-flex",
    alignItems: "center",
    whiteSpace: "nowrap",
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
  review: { marginTop: tokens.spacingVerticalS },
  reviewLink: {
    display: "inline-flex",
    alignItems: "center",
    whiteSpace: "nowrap",
    padding: `${tokens.spacingVerticalXS} ${tokens.spacingHorizontalM}`,
    borderRadius: tokens.borderRadiusMedium,
    textDecoration: "none",
    color: tokens.colorNeutralForeground1,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke1}`,
    fontWeight: tokens.fontWeightSemibold,
    ":hover": { backgroundColor: tokens.colorNeutralBackground1Hover },
  },
  navLinkActive: {
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground2,
    fontWeight: tokens.fontWeightSemibold,
  },
});

/** The project workspace, in the order a recap actually moves through it. */
const SECTIONS: readonly (readonly [path: string, label: string])[] = [
  ["", "Dashboard"],
  ["/transcript", "Transcript"],
  ["/script", "Script"],
  ["/storyboard", "Storyboard"],
  ["/references", "References"],
  ["/review", "Review"],
];

/** The project statuses that mean "nothing moves until a person decides". */
const REVIEW_STATUSES: ReadonlySet<string> = new Set([
  "shot_generation_failed",
  "awaiting_review",
]);

export interface ReviewPrompt {
  /** Shots whose QA raised an ambiguity for a person to settle. */
  readonly review: number;
  /** Shots that failed on judgement, which a person may still override. */
  readonly failed: number;
}

function reviewMessage({ review, failed }: ReviewPrompt): string {
  const parts: string[] = [];
  if (review > 0) {
    parts.push(`${review} shot${review === 1 ? "" : "s"} need${review === 1 ? "s" : ""} review`);
  }
  if (failed > 0) {
    parts.push(`${failed} shot${failed === 1 ? "" : "s"} failed`);
  }
  return parts.length > 0
    ? `${parts.join(", ")}. Approve or reject them on the storyboard to let the project continue.`
    : "This project is waiting on a review decision before it can continue.";
}

export interface ProjectStatusHeaderProps {
  readonly projectId: string;
  readonly projectName: string;
  readonly pageTitle: string;
  readonly workflow?: WorkflowStatusProjection | undefined;
  readonly connectionLabel?: string | undefined;
  readonly actions?: ReactNode;
  /**
   * Shots waiting on a human decision. A paused project says so here, with a
   * way through: without it the dashboard reports a failed stage and leaves
   * the owner to find the buttons on their own.
   */
  readonly reviewPrompt?: ReviewPrompt | undefined;
}

export function ProjectStatusHeader({
  projectId,
  projectName,
  pageTitle,
  workflow,
  connectionLabel,
  actions,
  reviewPrompt,
}: ProjectStatusHeaderProps): JSX.Element {
  const styles = useStyles();
  const { pathname } = useLocation();
  const base = `/projects/${projectId}`;
  const storyboard = `${base}/storyboard`;
  const waiting = workflow !== undefined && REVIEW_STATUSES.has(workflow.status);
  const pending = (reviewPrompt?.review ?? 0) + (reviewPrompt?.failed ?? 0);
  // The prompt is worth showing either because shots are actually waiting, or
  // because the project stopped at a review and the counts have not loaded.
  const showReview = pending > 0 || (waiting && reviewPrompt === undefined);

  return (
    <div className={styles.header}>
      <Breadcrumb aria-label="Breadcrumb" size="small">
        <BreadcrumbItem>
          <BreadcrumbButton as="a" href="/projects" {...{ to: "/projects" }}>
            Projects
          </BreadcrumbButton>
        </BreadcrumbItem>
        <BreadcrumbDivider />
        <BreadcrumbItem>
          <Link className={styles.crumbLink} to={base}>
            {projectName}
          </Link>
        </BreadcrumbItem>
        <BreadcrumbDivider />
        <BreadcrumbItem>
          <Body1>{pageTitle}</Body1>
        </BreadcrumbItem>
      </Breadcrumb>

      <div className={styles.row}>
        <h1 className={styles.title}>{pageTitle}</h1>
        {workflow !== undefined && <StatusBadge status={workflow.status} />}
        <div className={styles.spacer} />
        {actions}
      </div>

      {workflow !== undefined && (
        <div className={styles.meta}>
          <Caption1 className={styles.metaItem}>
            <FlowRegular aria-hidden="true" />
            Stage: <span className={styles.metaValue}>{formatStage(workflow.current_stage)}</span>
          </Caption1>
          <Caption1 className={styles.metaItem}>
            <VideoClipMultipleRegular aria-hidden="true" />
            Shots locked:{" "}
            <span className={styles.metaValue}>
              {workflow.completed_shot_count} of {workflow.total_shot_count}
            </span>
          </Caption1>
          <Caption1 className={styles.metaItem}>
            <ClockRegular aria-hidden="true" />
            Elapsed:{" "}
            <span className={styles.metaValue}>
              {formatDurationSeconds(workflow.elapsed_seconds)}
            </span>
          </Caption1>
          {connectionLabel !== undefined && (
            <Caption1
              className={styles.live}
              aria-label={`Live updates: ${connectionLabel}`}
            >
              <span
                aria-hidden="true"
                className={mergeClasses(
                  styles.liveDot,
                  connectionLabel === "live" && styles.liveDotOn,
                  (connectionLabel === "connecting" || connectionLabel === "reconnecting") &&
                    styles.liveDotWaiting,
                )}
              />
              Live updates: {connectionLabel}
            </Caption1>
          )}
        </div>
      )}

      {showReview && (
        <MessageBar intent="warning" className={styles.review}>
          <MessageBarBody>
            <MessageBarTitle>Keyframes need review</MessageBarTitle>
            {reviewMessage(reviewPrompt ?? { review: 0, failed: 0 })}
          </MessageBarBody>
          {!pathname.startsWith(storyboard) && (
            <MessageBarActions>
              <Link className={styles.reviewLink} to={storyboard}>
                Go to Storyboard
              </Link>
            </MessageBarActions>
          )}
        </MessageBar>
      )}

      <nav className={styles.nav} aria-label="Project sections">
        {SECTIONS.map(([suffix, label]) => {
          const to = `${base}${suffix}`;
          const active = suffix === "" ? pathname === base : pathname.startsWith(to);
          return (
            <Link
              key={label}
              to={to}
              className={mergeClasses(styles.navLink, active && styles.navLinkActive)}
              aria-current={active ? "page" : undefined}
            >
              {label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
