import {
  Badge,
  Body1,
  Button,
  Caption1,
  Divider,
  Subtitle2,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Title3,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type {
  VisualQAEvidenceProjection,
  VisualQARunDetailProjection,
  VisualQARunProjection,
  VisualQASampleProjection,
} from "@vidgen/contracts";

import { formatMicroseconds, formatMoney, humanize } from "../state/format";
import { StatusBadge } from "./StatusBadge";
import { TechnicalDetails } from "./TechnicalDetails";
import { VisualQAComparisonPanel } from "./VisualQAComparisonPanel";
import { VisualQAEvidenceViewer } from "./VisualQAEvidenceViewer";
import { VisualQAScorecard } from "./VisualQAScorecard";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  meta: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  scroll: { overflowX: "auto" },
});

export interface VisualQAResultPanelProps {
  readonly runs: readonly VisualQARunProjection[];
  readonly selected: VisualQARunDetailProjection | null;
  readonly evidence: readonly VisualQAEvidenceProjection[];
  readonly evidenceSamples: readonly VisualQASampleProjection[];
  readonly busy: boolean;
  readonly onSelectRun: (qaRunId: string) => void;
  readonly onRunQa: () => void;
  readonly onApprove: () => void;
  readonly onReject: () => void;
  readonly resolveAssetUrl: (assetId: string) => Promise<string | null>;
}

/**
 * The T20 panel of the shot inspector: keyframe and video outcomes, the
 * recomputed score, deterministic diagnostics, repair codes, adjudication and
 * human-review status, plus the evidence and comparison views.
 *
 * The recommended repair family is informational only. T20 never executes a
 * repair, so this panel deliberately offers no regeneration action.
 */
export function VisualQAResultPanel({
  runs,
  selected,
  evidence,
  evidenceSamples,
  busy,
  onSelectRun,
  onRunQa,
  onApprove,
  onReject,
  resolveAssetUrl,
}: VisualQAResultPanelProps): JSX.Element {
  const styles = useStyles();
  const keyframe = runs.find((run) => run.target_type === "keyframe") ?? null;
  const video = runs.find((run) => run.target_type === "video") ?? null;
  return (
    <section className={styles.wrapper} aria-labelledby="visual-qa-panel-heading">
      <Title3 as="h2" id="visual-qa-panel-heading">
        Visual QA
      </Title3>
      <div className={styles.meta}>
        <OutcomeBadge label="Keyframe QA" run={keyframe} />
        <OutcomeBadge label="Video QA" run={video} />
      </div>

      {runs.length === 0 && <Body1>No visual-QA result has been recorded for this shot.</Body1>}

      <div className={styles.actions}>
        <Button appearance="secondary" onClick={onRunQa} disabled={busy}>
          Run or resume visual QA
        </Button>
      </div>

      {runs.length > 0 && (
        <div className={styles.scroll}>
          <Table aria-label="Visual QA results for this shot" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Target</TableHeaderCell>
                <TableHeaderCell>Outcome</TableHeaderCell>
                <TableHeaderCell>Score / threshold</TableHeaderCell>
                <TableHeaderCell>Repair codes</TableHeaderCell>
                <TableHeaderCell>Action</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.qa_run_id}>
                  <TableCell>{humanize(run.target_type)}</TableCell>
                  <TableCell>
                    <StatusBadge status={run.outcome ?? run.status} />
                    {run.hard_failure && (
                      <Badge appearance="filled" color="danger">
                        Hard failure
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    {run.score === null ? "—" : run.score.toFixed(2)} /{" "}
                    {run.pass_threshold === null ? "—" : run.pass_threshold.toFixed(0)}
                  </TableCell>
                  <TableCell>{run.repair_codes.join(", ") || "—"}</TableCell>
                  <TableCell>
                    <Button
                      size="small"
                      appearance="secondary"
                      onClick={() => onSelectRun(run.qa_run_id)}
                      disabled={busy || selected?.qa_run_id === run.qa_run_id}
                    >
                      Inspect
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {selected !== null && (
        <>
          <Divider />
          <VisualQAScorecard run={selected} dimensions={selected.dimensions} />

          <Subtitle2 as="h3">Repair recommendation</Subtitle2>
          <Body1>
            {selected.repair_recommendation === null || selected.repair_recommendation === "NONE"
              ? "No repair is recommended."
              : `Recommended repair family: ${humanize(selected.repair_recommendation)}.`}{" "}
            This is informational: T20 identifies repairs and never performs one.
          </Body1>
          <Caption1>Repair codes: {selected.repair_codes.join(", ") || "none"}</Caption1>

          <Subtitle2 as="h3">Deterministic diagnostics</Subtitle2>
          <div className={styles.scroll}>
            <Table aria-label="Deterministic diagnostics" size="small">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Check</TableHeaderCell>
                  <TableHeaderCell>Outcome</TableHeaderCell>
                  <TableHeaderCell>Measured</TableHeaderCell>
                  <TableHeaderCell>Threshold</TableHeaderCell>
                  <TableHeaderCell>Evidence</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {selected.diagnostics.map((item) => (
                  <TableRow key={`${item.code}-${item.diagnostic_code}`}>
                    <TableCell>{humanize(item.code)}</TableCell>
                    <TableCell>{humanize(item.outcome)}</TableCell>
                    <TableCell>
                      {item.measurement === null ? "—" : item.measurement.toFixed(3)}
                    </TableCell>
                    <TableCell>
                      {item.threshold === null ? "—" : item.threshold.toFixed(3)}
                    </TableCell>
                    <TableCell>
                      {item.evidence_timestamp_us === null
                        ? "whole file"
                        : formatMicroseconds(item.evidence_timestamp_us)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <Subtitle2 as="h3">Adjudication and review</Subtitle2>
          <Body1>
            {selected.adjudication === null
              ? "First-pass evaluation only; adjudication was not required."
              : `Adjudicated by ${selected.adjudication.adjudicator_model} at confidence ` +
                `${selected.adjudication.adjudicator_confidence.toFixed(2)}; ` +
                `${selected.adjudication.decided ? "decided" : "not decided"}.`}
          </Body1>
          <Caption1>
            Human review:{" "}
            {selected.human_review_decision === null
              ? "not required"
              : humanize(selected.human_review_decision)}
          </Caption1>
          {selected.outcome === "REVIEW" && (
            <div className={styles.actions}>
              <Button appearance="primary" onClick={onApprove} disabled={busy}>
                Approve after review
              </Button>
              <Button appearance="secondary" onClick={onReject} disabled={busy}>
                Reject after review
              </Button>
            </div>
          )}

          <VisualQAEvidenceViewer
            evidence={evidence}
            samples={evidenceSamples.length > 0 ? evidenceSamples : selected.samples}
            resolveAssetUrl={resolveAssetUrl}
          />

          <VisualQAComparisonPanel
            sample={selected.samples[0] ?? null}
            referenceAssetIds={selected.compared_reference_asset_ids}
            resolveAssetUrl={resolveAssetUrl}
          />

          <TechnicalDetails
            title="Visual QA: technical details"
            entries={[
              ["QA run ID", selected.qa_run_id],
              ["Provider", selected.provider],
              ["Model", selected.model],
              ["Rubric version", selected.rubric_version],
              ["Threshold version", selected.threshold_version],
              ["Sampling version", selected.sampling_version],
              ["Samples", String(selected.sample_count)],
              ["Deterministic warnings", String(selected.deterministic_warning_count)],
              ["QA cost", formatMoney(String(selected.cost_microusd / 1_000_000))],
            ]}
          />
        </>
      )}
    </section>
  );
}

function OutcomeBadge({
  label,
  run,
}: {
  readonly label: string;
  readonly run: VisualQARunProjection | null;
}): JSX.Element {
  if (run === null) {
    return <Caption1>{label}: not run</Caption1>;
  }
  return (
    <Caption1>
      {label}: <StatusBadge status={run.outcome ?? run.status} />
      {run.score !== null && ` ${run.score.toFixed(2)}`}
      {run.pass_threshold !== null && ` / ${run.pass_threshold.toFixed(0)}`}
    </Caption1>
  );
}
