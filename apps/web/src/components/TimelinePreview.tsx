import { Caption1, Subtitle2, Tooltip, makeStyles, tokens } from "@fluentui/react-components";
import type { JSX } from "react";
import type { StoryboardShotProjection } from "@vidgen/contracts";

import { formatMicroseconds, formatTimecode } from "../state/format";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  scroll: { overflowX: "auto", paddingBottom: tokens.spacingVerticalXS },
  track: { display: "flex", minWidth: "600px", gap: "2px", listStyle: "none", margin: 0, padding: 0 },
  segment: {
    height: "44px",
    display: "grid",
    placeItems: "center",
    borderRadius: tokens.borderRadiusSmall,
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorNeutralForeground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    cursor: "pointer",
    minWidth: "24px",
  },
  active: { backgroundColor: tokens.colorBrandBackground, color: tokens.colorNeutralForegroundOnBrand },
});

export interface TimelinePreviewProps {
  readonly shots: readonly StoryboardShotProjection[];
  readonly totalDurationUs: number;
  readonly selectedShotId: string | null;
  readonly onSelect: (shotId: string) => void;
}

/** A read-only proportional timeline built from the T13 timing manifest. */
export function TimelinePreview({
  shots,
  totalDurationUs,
  selectedShotId,
  onSelect,
}: TimelinePreviewProps): JSX.Element {
  const styles = useStyles();
  const total = totalDurationUs > 0 ? totalDurationUs : 1;
  return (
    <section className={styles.wrapper} aria-labelledby="timeline-heading">
      <Subtitle2 as="h2" id="timeline-heading">
        Timeline
      </Subtitle2>
      <Caption1>
        Total {formatMicroseconds(totalDurationUs)} across {shots.length} shots. Timing is owned by
        the storyboard manifest and is read-only here.
      </Caption1>
      <div className={styles.scroll}>
        <ul className={styles.track} aria-label="Shot timeline">
          {shots.map((shot) => (
            <li key={shot.shot_id} style={{ display: "flex", flexGrow: shot.usable_duration_us / total }}>
            <Tooltip
              relationship="label"
              content={`Shot ${shot.global_sequence + 1}: ${formatTimecode(
                shot.global_start_us,
              )} – ${formatTimecode(shot.global_end_us)}`}
            >
              <button
                type="button"
                className={
                  shot.shot_id === selectedShotId
                    ? `${styles.segment} ${styles.active}`
                    : styles.segment
                }
                style={{ flexGrow: 1 }}
                onClick={() => onSelect(shot.shot_id)}
                aria-label={`Shot ${shot.global_sequence + 1}, ${formatMicroseconds(
                  shot.usable_duration_us,
                )}`}
              >
                {shot.global_sequence + 1}
              </button>
            </Tooltip>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
