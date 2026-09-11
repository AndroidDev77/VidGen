import { Body1, Button, Caption1, makeStyles, tokens } from "@fluentui/react-components";
import type { JSX } from "react";

import type { QaReviewCounts } from "../state/visualQa";

const useStyles = makeStyles({
  bar: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalL,
    flexWrap: "wrap",
    padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalL}`,
    borderRadius: tokens.borderRadiusLarge,
    border: `1px solid ${tokens.colorPaletteYellowBorder1}`,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  text: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: "260px" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
});

export interface StoryboardReviewBarProps {
  readonly counts: QaReviewCounts;
  readonly busy: boolean;
  /** Approve every shot whose QA raised an ambiguity. */
  readonly onApproveReviewable: () => void;
  /** Override every shot that failed on judgement rather than on a defect. */
  readonly onForceApproveFailed: () => void;
}

/**
 * One decision for a whole storyboard.
 *
 * A twenty-shot recap that stops on twenty separate QA outcomes is not twenty
 * decisions an owner wants to make one card at a time, so the two decisions
 * that are genuinely bulk - "these ambiguities are fine" and "override these
 * soft failures" - are offered together. Rejection stays per shot: it starts
 * paid regeneration, and nobody should trigger twenty of those with one click.
 */
export function StoryboardReviewBar({
  counts,
  busy,
  onApproveReviewable,
  onForceApproveFailed,
}: StoryboardReviewBarProps): JSX.Element | null {
  const styles = useStyles();
  if (counts.review === 0 && counts.failed === 0) {
    return null;
  }
  return (
    <section className={styles.bar} aria-label="Shots awaiting review">
      <div className={styles.text}>
        <Body1>
          {counts.review} shot{counts.review === 1 ? "" : "s"} need
          {counts.review === 1 ? "s" : ""} review, {counts.failed} shot
          {counts.failed === 1 ? "" : "s"} failed.
        </Body1>
        <Caption1>
          A hard failure is never listed here: it is a measured defect and cannot be approved.
        </Caption1>
      </div>
      <div className={styles.actions}>
        <Button
          appearance="primary"
          disabled={busy || counts.review === 0}
          onClick={onApproveReviewable}
        >
          Approve all reviewable ({counts.review})
        </Button>
        <Button
          appearance="secondary"
          disabled={busy || counts.failed === 0}
          onClick={onForceApproveFailed}
        >
          Force approve all failed ({counts.failed})
        </Button>
      </div>
    </section>
  );
}
