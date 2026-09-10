import { Body1, Caption1, ProgressBar, makeStyles, tokens } from "@fluentui/react-components";
import { DocumentSearchRegular } from "@fluentui/react-icons";
import type { JSX } from "react";

import type { EpisodeAnalysisProgress } from "../api/projects";
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
});

export interface EpisodeAnalysisProgressPanelProps {
  readonly progress: EpisodeAnalysisProgress;
}

/**
 * The episode-analysis bar on the dashboard.
 *
 * It renders whatever the last status response said and nothing else: while
 * the dashboard polls, a refresh in flight must not swap the bar for a
 * skeleton, so the panel has no loading state of its own.
 */
export function EpisodeAnalysisProgressPanel({
  progress,
}: EpisodeAnalysisProgressPanelProps): JSX.Element {
  const styles = useStyles();
  const failed = progress.phase === "failed";
  const finished = progress.phase === "completed";
  return (
    <SectionCard
      title="Episode analysis"
      icon={<DocumentSearchRegular />}
      description="Scenes are analyzed one by one, then combined into one episode model and checked."
    >
      <div className={styles.stack} data-testid="episode-analysis-progress">
        <div className={styles.row}>
          <Body1
            role={failed ? "alert" : "status"}
            className={failed ? styles.failure : undefined}
            data-testid="episode-analysis-message"
          >
            {progress.message}
          </Body1>
          <Caption1 className={styles.muted}>{Math.round(progress.percentage)}%</Caption1>
        </div>
        <ProgressBar
          value={progress.percentage}
          max={100}
          color={failed ? "error" : finished ? "success" : "brand"}
          aria-label="Episode analysis progress"
        />
        <div className={styles.row}>
          <Caption1 className={styles.muted}>
            {progress.completed_scene_count} of {progress.total_scene_count} scene
            {progress.total_scene_count === 1 ? "" : "s"} analyzed
          </Caption1>
          <Caption1 className={styles.muted}>
            Updated {formatTimestamp(progress.updated_at)}
          </Caption1>
        </div>
      </div>
    </SectionCard>
  );
}
