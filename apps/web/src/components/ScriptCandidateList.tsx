import {
  Badge,
  Body1,
  Button,
  Caption1,
  Subtitle2,
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
}

/**
 * The scripts a stalled generation run left behind, offered for approval.
 *
 * When the pipeline exhausts its revision attempts it stops without approving
 * a version, so `GET /script` has nothing to answer with. The candidates are
 * still there, and approving one is what unblocks the run — this list is that
 * choice, with each version readable in place first.
 */
export function ScriptCandidateList({
  projectId,
  candidates,
  selectingScriptId,
  onSelect,
}: ScriptCandidateListProps): JSX.Element {
  const styles = useStyles();
  return (
    <SectionCard
      title="Available scripts"
      description="Read a version, then approve the one the rest of the pipeline should build on."
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
                <Badge appearance="tint">{candidate.status}</Badge>
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
              <ScriptCandidatePreview projectId={projectId} scriptId={candidate.script_id} />
              <div className={styles.actions}>
                <Button
                  appearance="primary"
                  disabled={selectingScriptId !== null}
                  onClick={() => onSelect(candidate.script_id)}
                >
                  {selectingScriptId === candidate.script_id ? "Approving…" : "Approve this script"}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
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
