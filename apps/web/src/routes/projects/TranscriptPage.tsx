import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TranscriptSegmentProjection } from "@vidgen/contracts";
import { useCallback, useState, type JSX } from "react";
import { useSearchParams } from "react-router-dom";

import { newIdempotencyKey } from "../../api/client";
import { VidGenApiError } from "../../api/errors";
import { queryKeys } from "../../api/queryKeys";
import { getTranscript, updateTranscriptSegment } from "../../api/transcripts";
import { useApiClient } from "../../app/apiContext";
import { ConfirmInvalidationDialog } from "../../components/ConfirmInvalidationDialog";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { TranscriptEditor, type TranscriptDraft } from "../../components/TranscriptEditor";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { useDraftState } from "../../state/editorState";
import { useProjectContext } from "./useProjectContext";

interface PendingEdit {
  readonly segment: TranscriptSegmentProjection;
  readonly draft: TranscriptDraft;
}

export function TranscriptPage(): JSX.Element {
  const client = useApiClient();
  const queryClient = useQueryClient();
  const { projectId, project, workflow, connectionLabel } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get("q") ?? "";
  const drafts = useDraftState<TranscriptDraft>();
  const [pending, setPending] = useState<PendingEdit | null>(null);

  const transcript = useQuery({
    queryKey: queryKeys.transcript(projectId),
    queryFn: ({ signal }) => getTranscript(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });

  const save = useMutation({
    mutationFn: ({ segment, draft }: PendingEdit) =>
      updateTranscriptSegment(
        projectId,
        segment.segment_id,
        {
          text: draft.text,
          speaker_label: draft.speakerLabel,
          confirm_invalidation: true,
        },
        segment.row_version,
        newIdempotencyKey(`transcript-${segment.segment_id}`),
        client,
      ).then((r) => r.data),
    onSuccess: (_, variables) => {
      drafts.clear(variables.segment.segment_id);
      drafts.resolveConflict(variables.segment.segment_id);
      setPending(null);
      // A transcript edit only affects the transcript and downstream summaries.
      void queryClient.invalidateQueries({ queryKey: queryKeys.transcript(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.script(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.render(projectId) });
    },
    onError: (error, variables) => {
      // Keep the unsaved draft so the user can compare it with the server text.
      if (error instanceof VidGenApiError) {
        drafts.markConflict(variables.segment.segment_id, variables.draft);
      }
      setPending(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.transcript(projectId) });
    },
  });

  const onSearchChange = useCallback(
    (value: string) => {
      const next = new URLSearchParams(searchParams);
      if (value === "") {
        next.delete("q");
      } else {
        next.set("q", value);
      }
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  return (
    <div>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data?.name ?? "Project"}
        pageTitle="Transcript"
        workflow={workflow.data}
        connectionLabel={connectionLabel}
      />

      {transcript.isPending && <LoadingState label="Loading the transcript" rows={5} />}
      {transcript.isError && (
        <ErrorState
          error={transcript.error}
          title="No transcript yet"
          onRetry={() => void transcript.refetch()}
        />
      )}
      {transcript.isSuccess && transcript.data.segments.length === 0 && (
        <EmptyState
          title="The transcript is empty"
          description="This project's transcript has no segments to review."
        />
      )}
      {transcript.isSuccess && transcript.data.segments.length > 0 && (
        <>
          {save.isError && <ErrorState error={save.error} />}
          <TranscriptEditor
            transcript={transcript.data}
            drafts={drafts}
            search={search}
            onSearchChange={onSearchChange}
            savingSegmentId={save.isPending ? (save.variables?.segment.segment_id ?? null) : null}
            onSave={(segment, draft) => setPending({ segment, draft })}
          />
          <TechnicalDetails
            entries={[
              ["Transcript ID", transcript.data.transcript_id],
              ["Version", transcript.data.version],
              ["Origin", transcript.data.origin],
              ["Row version", transcript.data.row_version],
              ["Source asset", transcript.data.source_asset_id],
            ]}
          />
        </>
      )}

      <ConfirmInvalidationDialog
        open={pending !== null}
        title="Save this transcript edit?"
        description={
          "Editing the transcript makes the generated script, narration, storyboard and render " +
          "stale. Nothing is regenerated until you ask for it."
        }
        invalidation={null}
        confirmLabel="Save the segment"
        busy={save.isPending}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (pending !== null) {
            save.mutate(pending);
          }
        }}
      />
    </div>
  );
}
