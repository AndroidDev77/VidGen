import {
  Caption1,
  ProgressBar,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type { StageTimelineEntry, WorkflowStatusProjection } from "@vidgen/contracts";

import { formatStage, humanize } from "../state/format";
import { StatusBadge } from "./StatusBadge";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  list: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
    gap: tokens.spacingHorizontalS,
  },
  item: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalXXS,
    padding: tokens.spacingVerticalS,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
  },
});

export interface StageTimelineProps {
  readonly workflow: WorkflowStatusProjection;
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
  return (
    <section className={styles.wrapper} aria-labelledby="stage-timeline-heading">
      <Subtitle2 as="h2" id="stage-timeline-heading">
        Pipeline stages
      </Subtitle2>
      {percentage === null ? (
        <Caption1>
          Shot progress is not measurable yet, so no percentage is shown.
        </Caption1>
      ) : (
        <>
          <ProgressBar
            value={percentage / 100}
            max={1}
            thickness="large"
            aria-labelledby="stage-progress-label"
          />
          <Caption1 id="stage-progress-label">
            {workflow.completed_shot_count} of {workflow.total_shot_count} shots locked (
            {percentage.toFixed(0)}%)
          </Caption1>
        </>
      )}
      <ol className={styles.list}>
        {workflow.stages.map((stage: StageTimelineEntry) => (
          <li key={stage.stage} className={styles.item}>
            <Caption1>{formatStage(stage.stage)}</Caption1>
            <StatusBadge status={stage.state} />
            {stage.detail_code !== null && <Caption1>{humanize(stage.detail_code)}</Caption1>}
          </li>
        ))}
      </ol>
    </section>
  );
}
