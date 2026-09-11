import {
  Badge,
  Body1,
  Button,
  Caption1,
  Card,
  CardHeader,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type { StoryboardShotProjection, VisualQARunProjection } from "@vidgen/contracts";

import { formatMicroseconds, formatMoney, formatTimecode, humanize } from "../state/format";
import { decidableRun, decisionAffordance } from "../state/visualQa";
import { StatusBadge } from "./StatusBadge";

const useStyles = makeStyles({
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
    gap: tokens.spacingHorizontalL,
    listStyle: "none",
    margin: 0,
    padding: 0,
  },
  card: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  meta: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap", alignItems: "center" },
  thumb: {
    width: "100%",
    aspectRatio: "16 / 9",
    objectFit: "cover",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground3,
  },
  placeholder: {
    width: "100%",
    aspectRatio: "16 / 9",
    display: "grid",
    placeItems: "center",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground3,
  },
  selected: { outline: `2px solid ${tokens.colorBrandStroke1}`, outlineOffset: "2px" },
  decide: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
});

export interface StoryboardGridProps {
  readonly shots: readonly StoryboardShotProjection[];
  readonly selectedShotId: string | null;
  readonly onSelect: (shotId: string) => void;
  /** Resolved keyframe previews, keyed by asset ID; requested just in time. */
  readonly previewUrls: ReadonlyMap<string, string>;
  /** The canonical T20 visual-QA runs for this project, keyed by shot ID. */
  readonly visualQaByShot?: ReadonlyMap<string, readonly VisualQARunProjection[]>;
  /**
   * Record a human decision on the shot's decidable QA run, without opening
   * the inspector first. Omit both to render the grid read-only.
   */
  readonly onApprove?: (shotId: string) => void;
  readonly onReject?: (shotId: string) => void;
  /** A decision is already in flight; the card's actions wait for it. */
  readonly busy?: boolean;
}

/**
 * The canonical shots in sequence.
 *
 * Timing comes from the T13 manifest and is presented read-only: T18 does not
 * reorder shots, because the backend cannot yet validate arbitrary re-timing
 * without breaking T13's coverage guarantees.
 */
export function StoryboardGrid({
  shots,
  selectedShotId,
  onSelect,
  previewUrls,
  visualQaByShot,
  onApprove,
  onReject,
  busy = false,
}: StoryboardGridProps): JSX.Element {
  const styles = useStyles();
  return (
    <ul className={styles.grid}>
      {shots.map((shot) => {
        const preview =
          shot.selected_keyframe_asset_id === null
            ? undefined
            : previewUrls.get(shot.selected_keyframe_asset_id);
        const isSelected = shot.shot_id === selectedShotId;
        const qaRuns = visualQaByShot?.get(shot.shot_id) ?? [];
        // Reviewing 20 shots one inspector at a time is the flow this
        // replaces: the decision a shot is waiting for is offered on the card.
        const affordance = decisionAffordance(decidableRun(qaRuns));
        return (
          <li key={shot.shot_id}>
            <Card className={isSelected ? `${styles.card} ${styles.selected}` : styles.card}>
              <CardHeader
                header={<Subtitle2 as="h3">Shot {shot.global_sequence + 1}</Subtitle2>}
                description={
                  <Caption1>
                    {formatTimecode(shot.global_start_us)} – {formatTimecode(shot.global_end_us)}
                  </Caption1>
                }
              />
              {preview === undefined ? (
                <div className={styles.placeholder}>
                  <Caption1>No keyframe selected yet</Caption1>
                </div>
              ) : (
                <img
                  className={styles.thumb}
                  src={preview}
                  alt={`Selected keyframe for shot ${shot.global_sequence + 1}`}
                />
              )}
              <div className={styles.meta}>
                <StatusBadge status={shot.workflow_status} />
                {shot.failure_code !== null && (
                  <Badge appearance="filled" color="danger">
                    {humanize(shot.failure_code)}
                  </Badge>
                )}
                {shot.warning_code !== null && (
                  <Badge appearance="outline" color="warning">
                    {humanize(shot.warning_code)}
                  </Badge>
                )}
                {qaRuns.map((run) => (
                  <Badge
                    key={run.qa_run_id}
                    appearance={run.outcome === "PASS" ? "outline" : "filled"}
                    color={
                      run.outcome === "PASS"
                        ? "success"
                        : run.outcome === "REVIEW"
                          ? "warning"
                          : "danger"
                    }
                  >
                    {`${humanize(run.target_type)} QA ${run.outcome ?? "pending"}`}
                  </Badge>
                ))}
              </div>
              <Body1>{shot.visual_objective}</Body1>
              <Caption1>
                Usable {formatMicroseconds(shot.usable_duration_us)} · generated{" "}
                {formatMicroseconds(shot.requested_generation_duration_us)} · trim{" "}
                {formatMicroseconds(shot.trim_start_us)} / {formatMicroseconds(shot.trim_end_us)}
              </Caption1>
              <Caption1>
                {shot.camera_framing !== null && `${humanize(shot.camera_framing)} framing`}
                {shot.camera_movement !== null && `, ${humanize(shot.camera_movement)} camera`}
                {shot.transition_out !== null && ` · ${humanize(shot.transition_out)} out`}
              </Caption1>
              <Caption1>
                {shot.character_references.join(", ") || "No character references"}
                {shot.location_reference !== null && ` · ${shot.location_reference}`}
              </Caption1>
              <Caption1>
                {shot.provider ?? "no provider"} / {shot.model ?? "no model"} ·{" "}
                {shot.attempt_count} attempts · {formatMoney(shot.cost_amount)}
              </Caption1>
              {affordance !== null && (onApprove !== undefined || onReject !== undefined) && (
                <div className={styles.decide}>
                  {onApprove !== undefined && (
                    <Button
                      size="small"
                      appearance="primary"
                      disabled={busy}
                      onClick={() => onApprove(shot.shot_id)}
                    >
                      {affordance === "override" ? "Force approve" : "Approve"} shot{" "}
                      {shot.global_sequence + 1}
                    </Button>
                  )}
                  {onReject !== undefined && (
                    <Button
                      size="small"
                      appearance="secondary"
                      disabled={busy}
                      onClick={() => onReject(shot.shot_id)}
                    >
                      Reject shot {shot.global_sequence + 1}
                    </Button>
                  )}
                </div>
              )}
              <Button appearance="secondary" onClick={() => onSelect(shot.shot_id)}>
                Inspect shot {shot.global_sequence + 1}
              </Button>
            </Card>
          </li>
        );
      })}
    </ul>
  );
}
