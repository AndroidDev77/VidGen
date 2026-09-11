import {
  Badge,
  Body1,
  Button,
  Caption1,
  Field,
  Subtitle2,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import type { ScriptSummaryProjection } from "@vidgen/contracts";
import { useState, type JSX } from "react";

import { queryKeys } from "../api/queryKeys";
import { getScriptVersion } from "../api/scripts";
import { useApiClient } from "../app/apiContext";
import { formatTimestamp } from "../state/format";
import { SectionCard } from "./Surface";
import { ErrorState, LoadingState } from "./states";

const useStyles = makeStyles({
  list: { listStyle: "none", margin: 0, padding: 0, display: "grid", gap: tokens.spacingVerticalM },
  candidate: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    padding: tokens.spacingHorizontalL,
    borderRadius: tokens.borderRadiusLarge,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
  },
  meta: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  // Rejecting is a written brief for the next pass, so the feedback box sits
  // with the decision rather than in a dialog the reader has to leave for.
  feedback: { display: "grid", gap: tokens.spacingVerticalS, maxWidth: "80ch" },
  reason: {
    padding: tokens.spacingHorizontalM,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground2,
  },
  // The preview is reading copy, not editing copy: a measured column keeps a
  // full script scannable while the reader compares it against a sibling.
  preview: {
    display: "grid",
    gap: tokens.spacingVerticalS,
    maxWidth: "80ch",
    padding: tokens.spacingHorizontalM,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground2,
  },
});

export interface ScriptCandidateListProps {
  readonly projectId: string;
  readonly candidates: readonly ScriptSummaryProjection[];
  readonly selectingScriptId: string | null;
  readonly onSelect: (scriptId: string) => void;
  /** The candidate whose rejection is in flight, if any. */
  readonly rejectingScriptId?: string | null;
  /**
   * Send an editing pass back with feedback. Offered only for a version that
   * is waiting on the reviewer, since that is the one the next pass revises.
   */
  readonly onReject?: (scriptId: string, reason: string) => void;
}

/** What each pipeline status means to the reader, where the raw word is unclear. */
const STATUS_LABELS: Readonly<Record<string, string>> = {
  pending_review: "awaiting your review",
};

/**
 * The scripts a generation run has produced so far, offered for review.
 *
 * Every editing pass stops here: the pipeline leaves the edited version
 * waiting on the reviewer, so `GET /script` has nothing to answer with.
 * Approving a version is what moves the project on to narration; rejecting
 * it with feedback is what the next pass is asked to fix. Each version is
 * readable in place first.
 */
export function ScriptCandidateList({
  projectId,
  candidates,
  selectingScriptId,
  onSelect,
  rejectingScriptId = null,
  onReject,
}: ScriptCandidateListProps): JSX.Element {
  const styles = useStyles();
  const busy = selectingScriptId !== null || rejectingScriptId !== null;
  return (
    <SectionCard
      title="Available scripts"
      description={
        "Read a version, then approve the one the rest of the pipeline should build on, or " +
        "send the latest pass back with feedback for the next one."
      }
    >
      {candidates.length === 0 ? (
        <Body1>
          The run did not leave a script behind. Retry script generation to produce one.
        </Body1>
      ) : (
        <ul className={styles.list}>
          {candidates.map((candidate) => (
            <li key={candidate.script_id} className={styles.candidate}>
              <div className={styles.meta}>
                <Subtitle2 as="h3">Version {candidate.version}</Subtitle2>
                <Badge appearance="tint">{STATUS_LABELS[candidate.status] ?? candidate.status}</Badge>
                {candidate.editing_pass > 0 && (
                  <Badge appearance="outline">Editing pass {candidate.editing_pass}</Badge>
                )}
                {candidate.selected && (
                  <Badge appearance="tint" color="success">
                    Selected
                  </Badge>
                )}
                <Caption1>
                  {candidate.actual_word_count} words of a {candidate.target_word_count}-word target
                </Caption1>
                <Caption1>{formatTimestamp(candidate.created_at)}</Caption1>
              </div>
              {candidate.rejection_reason !== null && (
                <Body1 as="p" className={styles.reason}>
                  Rejected: {candidate.rejection_reason}
                </Body1>
              )}
              <ScriptCandidatePreview projectId={projectId} scriptId={candidate.script_id} />
              <div className={styles.actions}>
                <Button
                  appearance="primary"
                  disabled={busy}
                  onClick={() => onSelect(candidate.script_id)}
                >
                  {selectingScriptId === candidate.script_id ? "Approving…" : "Approve this script"}
                </Button>
              </div>
              {onReject !== undefined && candidate.status === "pending_review" && (
                <ScriptRejection
                  scriptId={candidate.script_id}
                  busy={busy}
                  rejecting={rejectingScriptId === candidate.script_id}
                  onReject={onReject}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

interface ScriptRejectionProps {
  readonly scriptId: string;
  readonly busy: boolean;
  readonly rejecting: boolean;
  readonly onReject: (scriptId: string, reason: string) => void;
}

/** The feedback the next editing pass is asked to address, and the button that sends it. */
function ScriptRejection({ scriptId, busy, rejecting, onReject }: ScriptRejectionProps): JSX.Element {
  const styles = useStyles();
  const [reason, setReason] = useState("");
  const trimmed = reason.trim();
  return (
    <div className={styles.feedback}>
      <Field
        label="What should the next editing pass fix?"
        hint="Your feedback goes to the Comedy Editor as the brief for the next pass."
      >
        <Textarea
          value={reason}
          resize="vertical"
          disabled={busy}
          onChange={(_event, data) => setReason(data.value)}
        />
      </Field>
      <div className={styles.actions}>
        <Button
          appearance="secondary"
          disabled={busy || trimmed === ""}
          onClick={() => onReject(scriptId, trimmed)}
        >
          {rejecting ? "Sending back…" : "Reject and run the next pass"}
        </Button>
      </div>
    </div>
  );
}

interface ScriptCandidatePreviewProps {
  readonly projectId: string;
  readonly scriptId: string;
}

/** One candidate's full text, fetched only once the reader asks for it. */
function ScriptCandidatePreview({
  projectId,
  scriptId,
}: ScriptCandidatePreviewProps): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const [open, setOpen] = useState(false);
  const preview = useQuery({
    queryKey: queryKeys.scriptVersion(projectId, scriptId),
    queryFn: ({ signal }) =>
      getScriptVersion(projectId, scriptId, client, signal).then((r) => r.data),
    enabled: open,
  });
  return (
    <div>
      <Button appearance="subtle" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide the script" : "Preview the script"}
      </Button>
      {open && (
        <div className={styles.preview}>
          {preview.isPending && <LoadingState label="Loading the script version" rows={3} />}
          {preview.isError && (
            <ErrorState
              error={preview.error}
              title="This version could not be loaded"
              onRetry={() => void preview.refetch()}
            />
          )}
          {preview.isSuccess &&
            preview.data.segments.map((segment) => (
              <Body1 key={segment.segment_id} as="p">
                {segment.text}
              </Body1>
            ))}
        </div>
      )}
    </div>
  );
}
