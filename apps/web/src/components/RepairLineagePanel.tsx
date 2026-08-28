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
  RepairAction,
  RepairAttemptProjection,
  RepairRunDetailProjection,
  RepairRunProjection,
} from "@vidgen/contracts";

import { formatMicroseconds, humanize } from "../state/format";
import { StatusBadge } from "./StatusBadge";
import { TechnicalDetails } from "./TechnicalDetails";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  meta: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  scroll: { overflowX: "auto" },
  clause: { margin: 0, paddingLeft: tokens.spacingHorizontalL },
});

export interface RepairLineagePanelProps {
  readonly runs: readonly RepairRunProjection[];
  readonly selected: RepairRunDetailProjection | null;
  readonly busy: boolean;
  readonly onSelectRun: (repairRunId: string) => void;
  readonly onAct: (action: RepairAction) => void;
}

/** Which actions a run in each state will accept, mirroring the API. */
const ALLOWED: Record<string, readonly RepairAction[]> = {
  REPAIR_PLANNING: ["retry", "cancel"],
  REPAIRING: ["retry", "cancel"],
  ALTERNATE_PROVIDER: ["retry", "cancel"],
  FALLBACK_RENDERING: ["retry", "cancel"],
  REVALIDATING: ["retry", "cancel"],
  HUMAN_REVIEW_REQUIRED: ["acknowledge", "resolve", "restart_after_reference_correction"],
  REPAIR_FAILED: ["restart_after_reference_correction"],
  LOCKED: [],
};

const ACTION_LABELS: Record<RepairAction, string> = {
  retry: "Resume the technical operation",
  cancel: "Cancel before the next paid attempt",
  acknowledge: "Acknowledge human review",
  resolve: "Resolve human review",
  restart_after_reference_correction: "Restart after upstream reference correction",
};

/**
 * The T21 panel of the shot inspector: the failure classification, the bounded
 * attempt lineage, what each attempt changed in the prompt, its cost, and the
 * fallback render when one was produced.
 *
 * The panel deliberately offers no "mark as passed" action. A hard-failing
 * visual can only be cleared by a repair attempt that produces its own new,
 * valid T20 result.
 */
export function RepairLineagePanel({
  runs,
  selected,
  busy,
  onSelectRun,
  onAct,
}: RepairLineagePanelProps): JSX.Element {
  const styles = useStyles();
  const actions = selected === null ? [] : (ALLOWED[selected.state] ?? []);
  return (
    <section className={styles.wrapper} aria-labelledby="repair-panel-heading">
      <Title3 as="h2" id="repair-panel-heading">
        Repair and fallback
      </Title3>

      {runs.length === 0 && (
        <Body1>No repair has been started for this shot. T20 passed, or T21 has not run yet.</Body1>
      )}

      {runs.length > 0 && (
        <div className={styles.scroll}>
          <Table aria-label="Repair runs for this shot" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>State</TableHeaderCell>
                <TableHeaderCell>Classification</TableHeaderCell>
                <TableHeaderCell>Repair code</TableHeaderCell>
                <TableHeaderCell>Score / threshold</TableHeaderCell>
                <TableHeaderCell>Attempts</TableHeaderCell>
                <TableHeaderCell>Action</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.repair_run_id}>
                  <TableCell>
                    <StatusBadge status={run.state} />
                    {run.hard_failure && (
                      <Badge appearance="filled" color="danger">
                        Hard failure
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    {run.failure_category === null ? "—" : humanize(run.failure_category)}
                    {run.failure_severity !== null && ` (${humanize(run.failure_severity)})`}
                  </TableCell>
                  <TableCell>{run.repair_code === null ? "—" : humanize(run.repair_code)}</TableCell>
                  <TableCell>
                    {run.qa_score === null ? "—" : run.qa_score.toFixed(2)} /{" "}
                    {run.pass_threshold === null ? "—" : run.pass_threshold.toFixed(0)}
                  </TableCell>
                  <TableCell>{run.total_attempt_count}</TableCell>
                  <TableCell>
                    <Button
                      size="small"
                      appearance="secondary"
                      onClick={() => onSelectRun(run.repair_run_id)}
                      disabled={busy || selected?.repair_run_id === run.repair_run_id}
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
          <div className={styles.meta}>
            <StatusBadge status={selected.state} />
            <Caption1>
              Hard failure: {selected.hard_failure_reason ?? (selected.hard_failure ? "yes" : "no")}
            </Caption1>
            {selected.human_review_reason !== null && (
              <Badge appearance="filled" color="warning">
                Human review: {humanize(selected.human_review_reason)}
              </Badge>
            )}
          </div>

          <Subtitle2 as="h3">Attempt lineage</Subtitle2>
          <div className={styles.scroll}>
            <Table aria-label="Repair attempt lineage" size="small">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>#</TableHeaderCell>
                  <TableHeaderCell>Kind</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Provider / model</TableHeaderCell>
                  <TableHeaderCell>Predecessor</TableHeaderCell>
                  <TableHeaderCell>QA</TableHeaderCell>
                  <TableHeaderCell>Estimated</TableHeaderCell>
                  <TableHeaderCell>Actual</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {selected.attempts.map((attempt) => (
                  <TableRow key={attempt.attempt_id}>
                    <TableCell>{attempt.attempt_ordinal}</TableCell>
                    <TableCell>{humanize(attempt.attempt_kind)}</TableCell>
                    <TableCell>
                      <StatusBadge status={attempt.status} />
                      {attempt.selected && (
                        <Badge appearance="filled" color="success">
                          Selected
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {attempt.provider || "—"} / {attempt.model || "—"}
                    </TableCell>
                    <TableCell>
                      {attempt.predecessor_attempt_id === null
                        ? "root"
                        : attempt.predecessor_attempt_id.slice(0, 8)}
                    </TableCell>
                    <TableCell>
                      {attempt.qa_score === null ? "—" : attempt.qa_score.toFixed(2)}
                      {attempt.qa_outcome !== null && ` (${attempt.qa_outcome})`}
                    </TableCell>
                    <TableCell>
                      {attempt.estimated_cost} {attempt.currency}
                    </TableCell>
                    <TableCell>
                      {attempt.actual_cost} {attempt.currency}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <Subtitle2 as="h3">Prompt changes</Subtitle2>
          {selected.attempts.filter((item) => item.prompt_delta !== null).length === 0 && (
            <Body1>No attempt changed the prompt.</Body1>
          )}
          {selected.attempts.map((attempt) => (
            <PromptDeltaView key={`delta-${attempt.attempt_id}`} attempt={attempt} />
          ))}

          <Subtitle2 as="h3">Cost and budget</Subtitle2>
          <Body1>
            Spent {selected.budget.total_repair_cost} {selected.budget.currency} of an estimated{" "}
            {selected.budget.estimated_repair_cost}.{" "}
            {selected.budget.per_shot_repair_cost_limit === null
              ? "No per-shot repair limit is configured."
              : `Per-shot repair limit ${selected.budget.per_shot_repair_cost_limit}.`}{" "}
            {selected.budget.project_remaining === null
              ? "No project budget is configured."
              : `Project budget remaining ${selected.budget.project_remaining}.`}
          </Body1>

          {selected.fallback !== null && (
            <>
              <Subtitle2 as="h3">Deterministic 2.5D fallback</Subtitle2>
              <Body1>
                {selected.fallback.renderer_version} rendered{" "}
                {formatMicroseconds(selected.fallback.exact_duration_us)} at{" "}
                {selected.fallback.width}x{selected.fallback.height}{" "}
                {selected.fallback.video_codec}/{selected.fallback.pixel_format} at{" "}
                {selected.fallback.frame_rate}.
              </Body1>
              <Caption1>
                Output asset {selected.fallback.output_asset_id}; manifest{" "}
                {selected.fallback.manifest_asset_id}.
              </Caption1>
            </>
          )}

          <Subtitle2 as="h3">Routing decisions</Subtitle2>
          <div className={styles.scroll}>
            <Table aria-label="Repair routing decisions" size="small">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>#</TableHeaderCell>
                  <TableHeaderCell>Route</TableHeaderCell>
                  <TableHeaderCell>Rationale</TableHeaderCell>
                  <TableHeaderCell>Estimated next</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {selected.decisions.map((decision) => (
                  <TableRow key={decision.decision_id}>
                    <TableCell>{decision.sequence}</TableCell>
                    <TableCell>{humanize(decision.route)}</TableCell>
                    <TableCell>{decision.rationale.join("; ")}</TableCell>
                    <TableCell>{decision.estimated_next_cost}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className={styles.actions}>
            {actions.map((action) => (
              <Button
                key={action}
                appearance="secondary"
                onClick={() => onAct(action)}
                disabled={busy}
              >
                {ACTION_LABELS[action]}
              </Button>
            ))}
            {actions.length === 0 && (
              <Caption1>
                This repair run is locked. A locked shot has already passed a fresh T20 evaluation.
              </Caption1>
            )}
          </div>

          <TechnicalDetails
            title="Repair policy and identities"
            entries={[
              ["Policy version", selected.policy_version],
              ["Planner version", selected.planner_version],
              ["Root animation attempt", selected.root_animation_attempt_id],
              ["Triggering QA result", selected.triggering_qa_result_id],
              ["Same-provider repairs used", String(selected.same_provider_repairs_used)],
              [
                "Alternate-provider attempts used",
                String(selected.alternate_provider_attempts_used),
              ],
              ["Fallback renders used", String(selected.fallback_renders_used)],
              ["Selected attempt", selected.selected_attempt_id ?? "none"],
              ["Selected output asset", selected.selected_asset_id ?? "none"],
              ["Final QA result", selected.final_qa_result_id ?? "none"],
            ]}
          />
        </>
      )}
    </section>
  );
}

function PromptDeltaView({ attempt }: { readonly attempt: RepairAttemptProjection }): JSX.Element {
  const styles = useStyles();
  const delta = attempt.prompt_delta;
  if (delta === null) {
    return <></>;
  }
  return (
    <div>
      <Caption1>
        Attempt {attempt.attempt_ordinal} ({humanize(attempt.attempt_kind)}) —{" "}
        {delta.repair_reason}
      </Caption1>
      <ul className={styles.clause}>
        {delta.added_clauses.map((clause) => (
          <li key={`add-${clause}`}>Added: {clause}</li>
        ))}
        {delta.removed_clauses.map((clause) => (
          <li key={`remove-${clause}`}>Removed: {clause}</li>
        ))}
        {delta.rewritten_clauses.map(([before, after]) => (
          <li key={`rewrite-${before}`}>
            Rewritten: {before} → {after}
          </li>
        ))}
        {delta.seed_changed && (
          <li key="seed">
            Seed changed from {delta.previous_seed ?? "none"} to {delta.new_seed ?? "none"}
          </li>
        )}
      </ul>
      <Caption1>
        Preserved {delta.preserved_constraint_ids.length} constraints; changed{" "}
        {delta.touched_constraint_ids.join(", ") || "none"}. Prompt hash{" "}
        {delta.before_prompt_hash.slice(0, 8)} → {delta.after_prompt_hash.slice(0, 8)}.
      </Caption1>
    </div>
  );
}
