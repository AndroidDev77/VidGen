import {
  Badge,
  Body1,
  Button,
  Caption1,
  Divider,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
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
import type {
  FinalCheckProjection,
  FinalCompletionGateProjection,
  FinalEditorialRunDetailProjection,
  FinalFindingProjection,
} from "@vidgen/contracts";
import type { JSX } from "react";

import { formatMicroseconds, formatTimecode, humanize } from "../state/format";
import { TechnicalDetails } from "./TechnicalDetails";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  meta: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  scroll: { overflowX: "auto" },
  timeline: {
    position: "relative",
    height: "28px",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground3,
    overflow: "hidden",
  },
  marker: { position: "absolute", top: 0, bottom: 0, minWidth: "3px" },
  facts: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
    gap: tokens.spacingHorizontalM,
  },
  fact: { display: "flex", flexDirection: "column" },
});

const SEVERITY_COLOR: Record<string, string> = {
  blocking: tokens.colorPaletteRedBackground3,
  review_required: tokens.colorPaletteYellowBackground3,
  warning: tokens.colorPaletteMarigoldBackground3,
  informational: tokens.colorNeutralBackground5,
};

export interface FinalQAPanelProps {
  readonly run: FinalEditorialRunDetailProjection | null;
  readonly gate: FinalCompletionGateProjection | null;
  readonly busy: boolean;
  readonly onRunFinalQa: () => void;
  readonly onCancel: () => void;
  readonly onResolveReview: (findingId: string) => void;
  readonly onRoute: (target: string, findingIds: readonly string[]) => void;
  readonly onOpenShot?: (shotId: string) => void;
}

/**
 * The T22 panel of the final-review page.
 *
 * Everything shown here is read from the persisted report, and the completion
 * verdict is the backend's own gate answer rather than something recomputed in
 * the browser. There is deliberately no control that marks a deterministic
 * failure as passed: only a genuinely uncertain semantic finding offers a
 * resolve action, and only when the gate says the run is eligible.
 */
export function FinalQAPanel({
  run,
  gate,
  busy,
  onRunFinalQa,
  onCancel,
  onResolveReview,
  onRoute,
  onOpenShot,
}: FinalQAPanelProps): JSX.Element {
  const styles = useStyles();
  if (run === null) {
    return (
      <section aria-labelledby="final-qa-heading" className={styles.wrapper}>
        <Title3 as="h2" id="final-qa-heading">
          Final editorial QA
        </Title3>
        <MessageBar intent="info">
          <MessageBarBody>
            <MessageBarTitle>Final QA has not run for this render</MessageBarTitle>
            The project cannot reach its completed state until final QA records a passing report
            for the current render.
          </MessageBarBody>
        </MessageBar>
        <div className={styles.actions}>
          <Button appearance="primary" disabled={busy} onClick={onRunFinalQa}>
            Run final QA
          </Button>
        </div>
      </section>
    );
  }

  const blocking = run.findings.filter((finding) => finding.blocking);
  const reviews = run.findings.filter(
    (finding) => finding.severity === "review_required" && !finding.resolved_by_review,
  );
  const warnings = run.findings.filter((finding) => finding.severity === "warning");
  const cancellable = [
    "FINAL_QA_QUEUED",
    "FINAL_QA_VALIDATING_INPUTS",
    "FINAL_QA_CHECKING_MEDIA",
    "FINAL_QA_CHECKING_CAPTIONS",
  ].includes(run.status);
  // A deterministic failure is a measured fact. No resolve action is offered
  // for one, and the whole run refuses semantic adjudication while one stands.
  const reviewEligible = run.deterministic_failure_count === 0 && run.error_code === null;

  return (
    <section aria-labelledby="final-qa-heading" className={styles.wrapper}>
      <Title3 as="h2" id="final-qa-heading">
        Final editorial QA
      </Title3>

      <div className={styles.meta}>
        <Badge
          appearance="filled"
          color={
            run.decision === "PASS" ? "success" : run.decision === "REVIEW" ? "warning" : "danger"
          }
        >
          {run.decision ?? humanize(run.status)}
        </Badge>
        <Caption1>Phase: {humanize(run.phase)}</Caption1>
        <Caption1>Status: {humanize(run.status)}</Caption1>
        <Caption1>
          {run.provider}/{run.model}
          {run.adjudicated ? " · adjudicated" : ""}
        </Caption1>
        <Caption1>Cost: {(run.cost_microusd / 1_000_000).toFixed(4)} USD</Caption1>
      </div>

      {gate !== null && !gate.allowed && (
        <MessageBar intent={gate.decision === "REVIEW" ? "warning" : "error"}>
          <MessageBarBody>
            <MessageBarTitle>The project cannot complete yet</MessageBarTitle>
            {humanize(gate.reason)}. {gate.blocking_finding_count} blocking,{" "}
            {gate.deterministic_failure_count} deterministic failure(s) and{" "}
            {gate.review_finding_count} unresolved review(s) on the current render.
          </MessageBarBody>
        </MessageBar>
      )}

      {run.deterministic_failure_count > 0 && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Deterministic failures cannot be overridden</MessageBarTitle>
            {run.deterministic_failure_count} measured media, audio or caption check(s) failed.
            These are facts, not judgement calls: produce a corrected render, then run final QA
            again.
          </MessageBarBody>
        </MessageBar>
      )}

      <FindingTimeline
        findings={run.findings}
        durationUs={run.timeline_duration_us}
        className={styles.timeline}
        markerClassName={styles.marker}
      />

      <div className={styles.facts}>
        <Fact label="Blocking" value={String(blocking.length)} />
        <Fact label="Review required" value={String(reviews.length)} />
        <Fact label="Warnings" value={String(warnings.length)} />
        <Fact
          label="Deterministic failures"
          value={String(run.deterministic_failure_count)}
        />
        <Fact label="Report version" value={run.report_version || "—"} />
        <Fact label="Input identity" value={run.final_qa_identity.slice(0, 16)} />
        {run.measurements !== null && (
          <>
            <Fact
              label="Measured duration"
              value={formatMicroseconds(run.measurements.container_duration_us)}
            />
            <Fact
              label="Resolution"
              value={
                run.measurements.width === null
                  ? "—"
                  : `${run.measurements.width}x${run.measurements.height}`
              }
            />
            <Fact
              label="Integrated loudness"
              value={
                run.measurements.integrated_lufs === null
                  ? "—"
                  : `${run.measurements.integrated_lufs.toFixed(1)} LUFS`
              }
            />
            <Fact
              label="True peak"
              value={
                run.measurements.true_peak_dbtp === null
                  ? "—"
                  : `${run.measurements.true_peak_dbtp.toFixed(1)} dBTP`
              }
            />
          </>
        )}
      </div>

      <div className={styles.actions}>
        <Button appearance="primary" disabled={busy} onClick={onRunFinalQa}>
          Rerun final QA
        </Button>
        <Button disabled={busy || !cancellable} onClick={onCancel}>
          Cancel before analysis
        </Button>
      </div>

      <Divider />
      <CheckTable title="Render checks" checks={run.media_checks} className={styles.scroll} />
      <CheckTable title="Audio checks" checks={run.audio_checks} className={styles.scroll} />
      <CheckTable title="Caption checks" checks={run.caption_checks} className={styles.scroll} />

      {run.dimensions.length > 0 && (
        <section aria-labelledby="final-qa-dimensions-heading" className={styles.scroll}>
          <Subtitle2 as="h3" id="final-qa-dimensions-heading">
            Editorial dimensions
          </Subtitle2>
          <Table size="small" aria-label="Editorial dimensions">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Dimension</TableHeaderCell>
                <TableHeaderCell>Score</TableHeaderCell>
                <TableHeaderCell>Confidence</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.dimensions.map((dimension) => (
                <TableRow key={dimension.category}>
                  <TableCell>{humanize(dimension.category)}</TableCell>
                  <TableCell>
                    {dimension.applicable ? dimension.score.toFixed(1) : "n/a"}
                  </TableCell>
                  <TableCell>{dimension.confidence.toFixed(2)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}

      <section aria-labelledby="final-qa-findings-heading" className={styles.scroll}>
        <Subtitle2 as="h3" id="final-qa-findings-heading">
          Findings
        </Subtitle2>
        {run.findings.length === 0 ? (
          <Body1>No findings were recorded for this render.</Body1>
        ) : (
          <Table size="small" aria-label="Final editorial findings">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Severity</TableHeaderCell>
                <TableHeaderCell>Issue</TableHeaderCell>
                <TableHeaderCell>Time</TableHeaderCell>
                <TableHeaderCell>Affected</TableHeaderCell>
                <TableHeaderCell>Evidence</TableHeaderCell>
                <TableHeaderCell>Remediation</TableHeaderCell>
                <TableHeaderCell>Action</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.findings.map((finding) => (
                <TableRow key={finding.finding_id}>
                  <TableCell>
                    <Badge
                      appearance="tint"
                      color={
                        finding.severity === "blocking"
                          ? "danger"
                          : finding.severity === "review_required"
                            ? "warning"
                            : "informative"
                      }
                    >
                      {humanize(finding.severity)}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {humanize(finding.issue_code)}
                    <Caption1 as="p">{finding.summary}</Caption1>
                  </TableCell>
                  <TableCell>
                    {formatTimecode(finding.start_us)}–{formatTimecode(finding.end_us)}
                  </TableCell>
                  <TableCell>
                    {finding.shot_ids.map((shotId) => (
                      <Button
                        key={shotId}
                        appearance="transparent"
                        size="small"
                        onClick={() => onOpenShot?.(shotId)}
                      >
                        {shotId.slice(0, 8)}
                      </Button>
                    ))}
                    {finding.caption_cue_sequences.length > 0 && (
                      <Caption1>cues {finding.caption_cue_sequences.join(", ")}</Caption1>
                    )}
                  </TableCell>
                  <TableCell>
                    {finding.evidence.length === 0
                      ? "—"
                      : finding.evidence
                          .map((item) =>
                            item.contact_sheet_position === null
                              ? (item.frame_asset_id ?? item.evidence_type)
                              : `tile ${item.contact_sheet_position}`,
                          )
                          .join(", ")}
                  </TableCell>
                  <TableCell>{humanize(finding.remediation_target)}</TableCell>
                  <TableCell>
                    {finding.severity === "review_required" &&
                    !finding.resolved_by_review &&
                    reviewEligible ? (
                      <Button
                        size="small"
                        disabled={busy}
                        onClick={() => onResolveReview(finding.finding_id)}
                      >
                        Resolve
                      </Button>
                    ) : finding.resolved_by_review ? (
                      <Caption1>Resolved</Caption1>
                    ) : (
                      <Caption1>—</Caption1>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      {run.remediation_routes.length > 0 && (
        <section aria-labelledby="final-qa-routes-heading" className={styles.wrapper}>
          <Subtitle2 as="h3" id="final-qa-routes-heading">
            Recommended remediation
          </Subtitle2>
          {run.remediation_routes.map((route) => (
            <div key={route.target} className={styles.meta}>
              <Body1>{humanize(route.target)}</Body1>
              <Caption1>{route.reason}</Caption1>
              {route.requires_new_render && (
                <Caption1>Requires a new render and a new final-QA run</Caption1>
              )}
              <Button
                size="small"
                disabled={busy}
                onClick={() => onRoute(route.target, route.finding_ids)}
              >
                Route
              </Button>
            </div>
          ))}
        </section>
      )}

      <TechnicalDetails
        title="Final QA provenance"
        entries={[
          ["Final QA run", run.final_editorial_run_id],
          ["Render identity", run.render_identity],
          ["Final QA identity", run.final_qa_identity],
          ["Input hash", run.input_hash],
          ["Configuration hash", run.configuration_hash],
          ["Report asset", run.report_asset_id],
          ["Contact sheet", run.contact_sheet_asset_id],
          ["Provider", `${run.provider}/${run.model}`],
          ["Row version", run.row_version],
        ]}
      />
    </section>
  );
}

/** Every finding as a marker on the render's own timeline. */
export function FindingTimeline({
  findings,
  durationUs,
  className,
  markerClassName,
}: {
  readonly findings: readonly FinalFindingProjection[];
  readonly durationUs: number;
  readonly className?: string;
  readonly markerClassName?: string;
}): JSX.Element {
  const total = durationUs > 0 ? durationUs : 1;
  return (
    <div className={className} role="img" aria-label={`${findings.length} final QA finding markers`}>
      {findings.map((finding) => {
        const left = Math.min(100, (finding.start_us / total) * 100);
        const width = Math.max(
          0.6,
          Math.min(100 - left, ((finding.end_us - finding.start_us) / total) * 100),
        );
        return (
          <div
            key={finding.finding_id}
            className={markerClassName}
            data-testid={`finding-marker-${finding.finding_id}`}
            title={`${finding.issue_code}: ${finding.summary}`}
            style={{
              left: `${left}%`,
              width: `${width}%`,
              backgroundColor: SEVERITY_COLOR[finding.severity] ?? tokens.colorNeutralBackground5,
            }}
          />
        );
      })}
    </div>
  );
}

function CheckTable({
  title,
  checks,
  className,
}: {
  readonly title: string;
  readonly checks: readonly FinalCheckProjection[];
  readonly className?: string;
}): JSX.Element | null {
  if (checks.length === 0) {
    return null;
  }
  const failures = checks.filter((check) => check.status === "fail");
  return (
    <section className={className}>
      <Subtitle2 as="h3">
        {title} ({failures.length} failing of {checks.length})
      </Subtitle2>
      <Table size="small" aria-label={title}>
        <TableHeader>
          <TableRow>
            <TableHeaderCell>Check</TableHeaderCell>
            <TableHeaderCell>Status</TableHeaderCell>
            <TableHeaderCell>Measured</TableHeaderCell>
            <TableHeaderCell>Threshold</TableHeaderCell>
            <TableHeaderCell>Detail</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {[...failures, ...checks.filter((check) => check.status !== "fail")].map((check) => (
            <TableRow key={check.check_id}>
              <TableCell>{humanize(check.code)}</TableCell>
              <TableCell>{check.status}</TableCell>
              <TableCell>
                {check.measurement === null ? "—" : `${check.measurement}${check.unit}`}
              </TableCell>
              <TableCell>
                {check.threshold === null ? "—" : `${check.threshold}${check.unit}`}
              </TableCell>
              <TableCell>{check.message}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

function Fact({ label, value }: { readonly label: string; readonly value: string }): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.fact}>
      <Caption1>{label}</Caption1>
      <Subtitle2>{value}</Subtitle2>
    </div>
  );
}
