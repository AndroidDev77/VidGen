import {
  Body1,
  Caption1,
  ProgressBar,
  makeStyles,
  mergeClasses,
  shorthands,
  tokens,
} from "@fluentui/react-components";
import { FlowRegular } from "@fluentui/react-icons";
import type { JSX } from "react";
import type { StageTimelineEntry, WorkflowStatusProjection } from "@vidgen/contracts";

import { formatStage, humanize } from "../state/format";
import { StatusBadge } from "./StatusBadge";
import { SectionCard } from "./Surface";

const useStyles = makeStyles({
  progress: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
  progressRow: {
    display: "flex",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: tokens.spacingHorizontalM,
  },
  progressValue: {
    fontSize: tokens.fontSizeBase400,
    fontWeight: tokens.fontWeightSemibold,
    fontVariantNumeric: "tabular-nums",
  },
  muted: { color: tokens.colorNeutralForeground3 },

  list: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "flex",
    flexDirection: "column",
  },
  // Each row hangs off a shared rail drawn by the connector below, so the
  // stages read as one ordered pipeline rather than as loose cards.
  item: {
    position: "relative",
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalM,
    paddingLeft: tokens.spacingHorizontalXL,
    paddingTop: tokens.spacingVerticalXS,
    paddingBottom: tokens.spacingVerticalXS,
    minHeight: "36px",
  },
  connector: {
    position: "absolute",
    left: "7px",
    top: 0,
    bottom: 0,
    width: "2px",
    backgroundColor: tokens.colorNeutralStroke2,
  },
  connectorFirst: { top: "50%" },
  connectorLast: { bottom: "50%" },
  // A single stage has no neighbours to connect to; drawing the rail anyway
  // would leave a stub hanging in empty space.
  connectorOnly: { display: "none" },
  dot: {
    position: "absolute",
    left: 0,
    width: "16px",
    height: "16px",
    borderRadius: tokens.borderRadiusCircular,
    border: `2px solid ${tokens.colorNeutralStroke1}`,
    backgroundColor: tokens.colorNeutralBackground1,
    boxSizing: "border-box",
  },
  dotDone: {
    ...shorthands.borderColor(tokens.colorPaletteGreenBorderActive),
    backgroundColor: tokens.colorPaletteGreenBorderActive,
  },
  dotActive: {
    ...shorthands.borderColor(tokens.colorBrandStroke1),
    backgroundColor: tokens.colorBrandBackground,
    boxShadow: `0 0 0 4px ${tokens.colorBrandBackground2}`,
  },
  dotFailed: {
    ...shorthands.borderColor(tokens.colorPaletteRedBorderActive),
    backgroundColor: tokens.colorPaletteRedBorderActive,
  },
  name: { flexGrow: 1, minWidth: 0 },
  namePending: { color: tokens.colorNeutralForeground3 },
  nameActive: { fontWeight: tokens.fontWeightSemibold },
  detail: { display: "block", color: tokens.colorNeutralForeground3 },
});

export interface StageTimelineProps {
  readonly workflow: WorkflowStatusProjection;
}

type DotState = "done" | "active" | "failed" | "pending";

/** Classify a stage for the rail marker only; the badge still states it in words. */
function dotState(state: string): DotState {
  if (["complete", "completed", "succeeded", "locked", "approved", "render_complete"].includes(state)) {
    return "done";
  }
  if (["failed", "cancelled", "render_failed", "render_cancelled"].includes(state)) {
    return "failed";
  }
  if (["pending", "queued", "render_queued", "superseded", "not_started"].includes(state)) {
    return "pending";
  }
  return "active";
}

/**
 * The pipeline stages in repository order.
 *
 * The percentage is shown only when the backend computed one from real shot
 * counts; it is never guessed from a status name.
 */
export function StageTimeline({ workflow }: StageTimelineProps): JSX.Element {
  const styles = useStyles();
  const percentage = workflow.progress_percentage;
  const stages = workflow.stages;
  return (
    <SectionCard title="Pipeline stages" icon={<FlowRegular />}>
      {percentage === null ? (
        <Caption1 className={styles.muted}>
          Shot progress is not measurable yet, so no percentage is shown.
        </Caption1>
      ) : (
        <div className={styles.progress}>
          <div className={styles.progressRow}>
            <Caption1 id="stage-progress-label" className={styles.muted}>
              {workflow.completed_shot_count} of {workflow.total_shot_count} shots locked
            </Caption1>
            <span className={styles.progressValue}>{percentage.toFixed(0)}%</span>
          </div>
          <ProgressBar
            value={percentage / 100}
            max={1}
            thickness="large"
            shape="rounded"
            aria-labelledby="stage-progress-label"
          />
        </div>
      )}
      <ol className={styles.list}>
        {stages.map((stage: StageTimelineEntry, index) => {
          const marker = dotState(stage.state);
          const only = stages.length === 1;
          return (
            <li key={stage.stage} className={styles.item}>
              <span
                aria-hidden="true"
                className={mergeClasses(
                  styles.connector,
                  only && styles.connectorOnly,
                  index === 0 && styles.connectorFirst,
                  index === stages.length - 1 && styles.connectorLast,
                )}
              />
              <span
                aria-hidden="true"
                className={mergeClasses(
                  styles.dot,
                  marker === "done" && styles.dotDone,
                  marker === "active" && styles.dotActive,
                  marker === "failed" && styles.dotFailed,
                )}
              />
              <span className={styles.name}>
                <Body1
                  className={mergeClasses(
                    marker === "pending" && styles.namePending,
                    marker === "active" && styles.nameActive,
                  )}
                >
                  {formatStage(stage.stage)}
                </Body1>
                {stage.detail_code !== null && (
                  <Caption1 className={styles.detail}>{humanize(stage.detail_code)}</Caption1>
                )}
              </span>
              <StatusBadge status={stage.state} />
            </li>
          );
        })}
      </ol>
    </SectionCard>
  );
}
