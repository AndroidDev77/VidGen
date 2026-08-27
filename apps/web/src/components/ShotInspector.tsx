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
import type { ShotAttemptProjection, ShotDetailProjection } from "@vidgen/contracts";

import { formatMicroseconds, formatMoney, formatTimecode, humanize } from "../state/format";
import { StatusBadge } from "./StatusBadge";
import { TechnicalDetails } from "./TechnicalDetails";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  meta: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  scroll: { overflowX: "auto" },
});

export interface ShotInspectorProps {
  readonly detail: ShotDetailProjection;
  readonly busy: boolean;
  readonly onRegenerate: () => void;
  readonly onRetry: () => void;
  readonly onCancel: () => void;
  readonly onSelectAttempt: (attemptId: string) => void;
  readonly onRefreshStatus: () => void;
}

export function ShotInspector({
  detail,
  busy,
  onRegenerate,
  onRetry,
  onCancel,
  onSelectAttempt,
  onRefreshStatus,
}: ShotInspectorProps): JSX.Element {
  const styles = useStyles();
  const shot = detail.shot;
  return (
    <section className={styles.wrapper} aria-labelledby="shot-inspector-heading">
      <Title3 as="h2" id="shot-inspector-heading">
        Shot {shot.global_sequence + 1}
      </Title3>
      <div className={styles.meta}>
        <StatusBadge status={detail.child_workflow_status} />
        {detail.child_workflow_retryable && (
          <Badge appearance="outline" color="warning">
            Retryable failure
          </Badge>
        )}
        <Caption1>
          {formatTimecode(shot.global_start_us)} – {formatTimecode(shot.global_end_us)}
        </Caption1>
        <Caption1>Attempts: {shot.attempt_count}</Caption1>
        <Caption1>Cost: {formatMoney(shot.cost_amount)}</Caption1>
      </div>

      <Body1>{shot.visual_objective}</Body1>
      <Caption1>
        Usable {formatMicroseconds(shot.usable_duration_us)} · generated{" "}
        {formatMicroseconds(shot.requested_generation_duration_us)} · trim start{" "}
        {formatMicroseconds(shot.trim_start_us)} · trim end {formatMicroseconds(shot.trim_end_us)}
      </Caption1>
      <Caption1>
        References: {shot.character_references.join(", ") || "none"}
        {shot.location_reference !== null && ` · location ${shot.location_reference}`}
      </Caption1>
      {detail.source_evidence_ids.length > 0 && (
        <Caption1>{detail.source_evidence_ids.length} source evidence references</Caption1>
      )}

      <div className={styles.actions}>
        <Button appearance="primary" onClick={onRegenerate} disabled={busy}>
          Regenerate this shot
        </Button>
        <Button
          appearance="secondary"
          onClick={onRetry}
          disabled={busy || !detail.child_workflow_retryable}
        >
          Retry failed shot
        </Button>
        <Button appearance="secondary" onClick={onCancel} disabled={busy}>
          Cancel this shot
        </Button>
        <Button appearance="subtle" onClick={onRefreshStatus} disabled={busy}>
          Refresh workflow status
        </Button>
      </div>

      <Divider />
      <AttemptTable
        title="Keyframe attempts"
        attempts={detail.keyframe_attempts}
        onSelect={undefined}
        busy={busy}
      />
      <AttemptTable
        title="Video attempts"
        attempts={detail.video_attempts}
        onSelect={onSelectAttempt}
        busy={busy}
      />

      {detail.regeneration_history.length > 0 && (
        <Caption1>
          {detail.regeneration_history.length} previous regeneration
          {detail.regeneration_history.length === 1 ? "" : "s"} recorded for this shot.
        </Caption1>
      )}

      <TechnicalDetails
        entries={[
          ["Shot ID", shot.shot_id],
          ["Stable shot ID", shot.stable_shot_id],
          ["Script segment", shot.script_segment_id],
          ["Child workflow", detail.child_workflow_id],
          ["Identity hash", detail.identity_hash],
          ["Trim instructions asset", detail.trim_instructions_asset_id],
          ["Provider", shot.provider],
          ["Model", shot.model],
        ]}
      />
    </section>
  );
}

function AttemptTable({
  title,
  attempts,
  onSelect,
  busy,
}: {
  readonly title: string;
  readonly attempts: readonly ShotAttemptProjection[];
  readonly onSelect: ((attemptId: string) => void) | undefined;
  readonly busy: boolean;
}): JSX.Element {
  const styles = useStyles();
  const captionId = `${title.replace(/\s+/g, "-").toLowerCase()}-caption`;
  return (
    <div className={styles.wrapper}>
      <Subtitle2 as="h3" id={captionId}>
        {title}
      </Subtitle2>
      {attempts.length === 0 ? (
        <Body1>No attempts recorded yet.</Body1>
      ) : (
        <div className={styles.scroll}>
          <Table aria-labelledby={captionId} size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>#</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
                <TableHeaderCell>Provider / model</TableHeaderCell>
                <TableHeaderCell>Durations</TableHeaderCell>
                <TableHeaderCell>Selected</TableHeaderCell>
                {onSelect !== undefined && <TableHeaderCell>Action</TableHeaderCell>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {attempts.map((attempt) => (
                <TableRow key={attempt.attempt_id}>
                  <TableCell>{attempt.attempt_number}</TableCell>
                  <TableCell>
                    <StatusBadge status={attempt.status} />
                    {attempt.failure_class !== null && (
                      <Caption1>{humanize(attempt.failure_class)}</Caption1>
                    )}
                  </TableCell>
                  <TableCell>
                    {attempt.provider} / {attempt.model}
                  </TableCell>
                  <TableCell>
                    {formatMicroseconds(attempt.generated_duration_us)} generated,{" "}
                    {formatMicroseconds(attempt.usable_duration_us)} usable
                  </TableCell>
                  <TableCell>
                    {attempt.selected ? (
                      <Badge appearance="filled" color="success">
                        Selected
                      </Badge>
                    ) : (
                      <Caption1>Not selected</Caption1>
                    )}
                  </TableCell>
                  {onSelect !== undefined && (
                    <TableCell>
                      <Button
                        size="small"
                        appearance="secondary"
                        disabled={busy || attempt.selected || attempt.status !== "succeeded"}
                        onClick={() => onSelect(attempt.attempt_id)}
                      >
                        Select
                      </Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {attempts.length > 0 && (
        <TechnicalDetails
          title={`${title}: provider identity`}
          entries={attempts.flatMap((attempt) => [
            [`Attempt ${attempt.attempt_number} task`, attempt.provider_task_id] as const,
            [`Attempt ${attempt.attempt_number} identity`, attempt.generation_identity] as const,
            [`Attempt ${attempt.attempt_number} prompt`, attempt.prompt_version] as const,
          ])}
        />
      )}
    </div>
  );
}
