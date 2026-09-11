import type { VisualQARunProjection } from "@vidgen/contracts";

/**
 * What a person can still decide about one T20 visual-QA run.
 *
 * `review` is the ambiguity the pipeline raised and asked a human to settle.
 * `override` is a soft failure - a score below the threshold, a prompt judged
 * too complex, an identity the model read wrong - that a human may overrule.
 *
 * A hard failure is a measured fact, and a failure carrying one is never
 * overridable. The review dialog refuses approval on a hard failure of its own
 * accord, and the API refuses it after that, so the two guards stay honest
 * even about a combination the pipeline cannot currently produce.
 */
export type QaDecisionAffordance = "review" | "override" | null;

export function decisionAffordance(
  run: Pick<VisualQARunProjection, "outcome" | "hard_failure"> | null | undefined,
): QaDecisionAffordance {
  if (!run) {
    return null;
  }
  if (run.outcome === "REVIEW") {
    return "review";
  }
  return run.outcome === "FAIL" && !run.hard_failure ? "override" : null;
}

/** The run a shot's decision applies to: the one a person can still act on. */
export function decidableRun(
  runs: readonly VisualQARunProjection[],
): VisualQARunProjection | null {
  // An ambiguity the pipeline raised outranks a failure it is sure about: it
  // is the cheaper decision and the one the pipeline actually asked for.
  return (
    runs.find((run) => decisionAffordance(run) === "review") ??
    runs.find((run) => decisionAffordance(run) === "override") ??
    null
  );
}

export interface QaReviewCounts {
  /** Shots whose latest actionable run is an ambiguous REVIEW. */
  readonly review: number;
  /** Shots whose latest actionable run is a soft FAIL a human may override. */
  readonly failed: number;
}

/** How many shots are waiting on a person, split by what they would be doing. */
export function reviewCounts(
  byShot: ReadonlyMap<string, readonly VisualQARunProjection[]>,
): QaReviewCounts {
  let review = 0;
  let failed = 0;
  for (const runs of byShot.values()) {
    const affordance = decisionAffordance(decidableRun(runs));
    if (affordance === "review") {
      review += 1;
    } else if (affordance === "override") {
      failed += 1;
    }
  }
  return { review, failed };
}
