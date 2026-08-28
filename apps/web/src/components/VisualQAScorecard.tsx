import {
  Badge,
  Body1,
  Caption1,
  ProgressBar,
  Subtitle2,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type { VisualQADimensionProjection, VisualQARunProjection } from "@vidgen/contracts";

import { humanize } from "../state/format";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  header: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  scroll: { overflowX: "auto" },
});

export interface VisualQAScorecardProps {
  readonly run: VisualQARunProjection;
  readonly dimensions: readonly VisualQADimensionProjection[];
}

/**
 * The recomputed weighted score, the applicable pass threshold, and every
 * rubric dimension. The total shown here is the score the application
 * recomputed; no provider-supplied total is ever displayed.
 */
export function VisualQAScorecard({ run, dimensions }: VisualQAScorecardProps): JSX.Element {
  const styles = useStyles();
  const total = run.score ?? 0;
  const threshold = run.pass_threshold ?? 0;
  const headingId = `visual-qa-scorecard-${run.qa_run_id}`;
  return (
    <section className={styles.wrapper} aria-labelledby={headingId}>
      <Subtitle2 as="h3" id={headingId}>
        Dimension scorecard
      </Subtitle2>
      <div className={styles.header}>
        <Badge appearance="filled" color={run.outcome === "PASS" ? "success" : "danger"}>
          {run.outcome ?? "pending"}
        </Badge>
        {run.hard_failure && (
          <Badge appearance="filled" color="danger">
            Hard failure blocks this shot
          </Badge>
        )}
        <Body1>
          Recomputed total {total.toFixed(2)} / pass threshold {threshold.toFixed(0)} (
          {humanize(run.importance)} shot)
        </Body1>
        {run.confidence !== null && <Caption1>Confidence {run.confidence.toFixed(2)}</Caption1>}
      </div>
      <ProgressBar
        value={Math.min(1, total / 100)}
        aria-label={`Recomputed visual QA score ${total.toFixed(2)} of 100`}
      />
      <div className={styles.scroll}>
        <Table aria-labelledby={headingId} size="small">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Dimension</TableHeaderCell>
              <TableHeaderCell>Raw</TableHeaderCell>
              <TableHeaderCell>Weight</TableHeaderCell>
              <TableHeaderCell>Contribution</TableHeaderCell>
              <TableHeaderCell>Confidence</TableHeaderCell>
              <TableHeaderCell>Codes</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {dimensions.map((dimension) => (
              <TableRow key={dimension.dimension}>
                <TableCell>{humanize(dimension.dimension)}</TableCell>
                <TableCell>
                  {dimension.applicable ? dimension.raw_score.toFixed(1) : "n/a"}
                </TableCell>
                <TableCell>{dimension.effective_weight.toFixed(2)}</TableCell>
                <TableCell>{dimension.weighted_contribution.toFixed(2)}</TableCell>
                <TableCell>{dimension.confidence.toFixed(2)}</TableCell>
                <TableCell>
                  {dimension.hard_failure_codes.length > 0 && (
                    <Badge appearance="filled" color="danger">
                      {dimension.hard_failure_codes.join(", ")}
                    </Badge>
                  )}
                  {dimension.hard_failure_codes.length === 0 &&
                    (dimension.repair_codes.join(", ") || "—")}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
