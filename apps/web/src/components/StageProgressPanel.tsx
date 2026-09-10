import { Body1, Caption1, ProgressBar, makeStyles, tokens } from "@fluentui/react-components";
import { DocumentSearchRegular } from "@fluentui/react-icons";
import type { JSX } from "react";

import type { StageProgress, StageProgressState } from "../api/projects";
import { formatTimestamp } from "../state/format";
import { SectionCard } from "./Surface";

const useStyles = makeStyles({
  stack: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  row: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "baseline",
    gap: tokens.spacingHorizontalM,
    flexWrap: "wrap",
  },
  muted: { color: tokens.colorNeutralForeground3 },
  failure: { color: tokens.colorPaletteRedForeground1 },
  waiting: { color: tokens.colorPaletteDarkOrangeForeground1 },
});

const BAR_COLORS: Record<StageProgressState, "brand" | "success" | "warning" | "error"> = {
  queued: "brand",
  running: "brand",
  waiting: "warning",
  completed: "success",
  failed: "error",
};

export interface StageProgressPanelProps {
  readonly progress: StageProgress;
}

/**
 * The progress bar for whichever stage the project is on.
 *
 * It renders whatever the last status response said and nothing else: while
 * the dashboard polls, a refresh in flight must not swap the bar for a
 * skeleton, so the panel has no loading state of its own. The heading,
 * message and count label all come from the response, so a new stage needs
 * no change here.
 */
export function StageProgressPanel({ progress }: StageProgressPanelProps): JSX.Element {
  const styles = useStyles();
  const failed = progress.state === "failed";
  const waiting = progress.state === "waiting";
  return (
    <SectionCard
      title={progress.label}
      icon={<DocumentSearchRegular />}
      description="Read from what the stage has saved so far, so it survives a worker restart."
    >
      <div className={styles.stack} data-testid="stage-progress">
        <div className={styles.row}>
          <Body1
            role={failed ? "alert" : "status"}
            className={failed ? styles.failure : waiting ? styles.waiting : undefined}
            data-testid="stage-progress-message"
          >
            {progress.message}
          </Body1>
          <Caption1 className={styles.muted}>{Math.round(progress.percentage)}%</Caption1>
        </div>
        <ProgressBar
          value={progress.percentage}
          max={100}
          color={BAR_COLORS[progress.state]}
          aria-label={`${progress.label} progress`}
        />
        <div className={styles.row}>
          <Caption1 className={styles.muted}>
            {progress.total_count > 0
              ? `${progress.completed_count} of ${progress.total_count} ${progress.count_label}`
              : ""}
          </Caption1>
          <Caption1 className={styles.muted}>
            Updated {formatTimestamp(progress.updated_at)}
          </Caption1>
        </div>
      </div>
    </SectionCard>
  );
}
