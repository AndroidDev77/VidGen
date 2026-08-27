import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageBar, MessageBarBody, MessageBarTitle } from "@fluentui/react-components";
import type { InvalidationSet, ScriptSegmentProjection } from "@vidgen/contracts";
import { useEffect, useState, type JSX } from "react";

import { newIdempotencyKey } from "../../api/client";
import { VidGenApiError } from "../../api/errors";
import { queryKeys } from "../../api/queryKeys";
import { getScript, listScripts, selectScript, updateScriptSegment } from "../../api/scripts";
import { useApiClient } from "../../app/apiContext";
import { ConfirmInvalidationDialog } from "../../components/ConfirmInvalidationDialog";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { ScriptEditor, type ScriptDraft } from "../../components/ScriptEditor";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { ErrorState, LoadingState } from "../../components/states";
import { useDraftState } from "../../state/editorState";
import { useProjectContext } from "./useProjectContext";

interface PendingEdit {
  readonly segment: ScriptSegmentProjection;
  readonly draft: ScriptDraft;
}

export function ScriptPage(): JSX.Element {
  const client = useApiClient();
  const queryClient = useQueryClient();
  const { projectId, project, workflow, connectionLabel } = useProjectContext();
  const drafts = useDraftState<ScriptDraft>();
  const [pending, setPending] = useState<PendingEdit | null>(null);
  const [lastInvalidation, setLastInvalidation] = useState<InvalidationSet | null>(null);

  const script = useQuery({
    queryKey: queryKeys.script(projectId),
    queryFn: ({ signal }) => getScript(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const versions = useQuery({
    queryKey: queryKeys.scripts(projectId),
    queryFn: ({ signal }) => listScripts(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });

  // Warn before a browser navigation discards unsaved script text.
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (drafts.isDirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [drafts.isDirty]);

  const save = useMutation({
    mutationFn: ({ segment, draft }: PendingEdit) =>
      updateScriptSegment(
        projectId,
        segment.segment_id,
        { text: draft.text, visual_gag: draft.visualGag, confirm_invalidation: true },
        segment.row_version,
        newIdempotencyKey(`script-${segment.segment_id}`),
        client,
      ).then((r) => r.data),
    onSuccess: (result, variables) => {
      drafts.clear(variables.segment.segment_id);
      drafts.resolveConflict(variables.segment.segment_id);
      setPending(null);
      setLastInvalidation(result.invalidation);
      // Script lineage plus the downstream summaries it makes stale.
      void queryClient.invalidateQueries({ queryKey: queryKeys.script(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.scripts(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.render(projectId) });
    },
    onError: (error, variables) => {
      if (error instanceof VidGenApiError) {
        drafts.markConflict(variables.segment.segment_id, variables.draft);
      }
      setPending(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.script(projectId) });
    },
  });

  const select = useMutation({
    mutationFn: (scriptId: string) => {
      const version = versions.data?.items.find((item) => item.script_id === scriptId);
      return selectScript(
        projectId,
        scriptId,
        version?.row_version ?? 1,
        newIdempotencyKey(`script-select-${scriptId}`),
        client,
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.script(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.scripts(projectId) });
    },
  });

  return (
    <div>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data?.name ?? "Project"}
        pageTitle="Script"
        workflow={workflow.data}
        connectionLabel={connectionLabel}
      />

      {script.isPending && <LoadingState label="Loading the script" rows={5} />}
      {script.isError && (
        <ErrorState
          error={script.error}
          title="No approved script yet"
          onRetry={() => void script.refetch()}
        />
      )}
      {save.isError && <ErrorState error={save.error} />}
      {select.isError && <ErrorState error={select.error} />}

      {lastInvalidation !== null && lastInvalidation.entries.length > 0 && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Downstream work is now stale</MessageBarTitle>
            {lastInvalidation.entries.map((entry) => entry.label).join(", ")}. Nothing was
            regenerated; start the stages you want when you are ready.
          </MessageBarBody>
        </MessageBar>
      )}

      {script.isSuccess && (
        <>
          <ScriptEditor
            script={script.data}
            versions={versions.data?.items ?? [script.data.script]}
            drafts={drafts}
            savingSegmentId={save.isPending ? (save.variables?.segment.segment_id ?? null) : null}
            onSave={(segment, draft) => setPending({ segment, draft })}
            onSelectVersion={(scriptId) => select.mutate(scriptId)}
          />
          <TechnicalDetails
            entries={[
              ["Script ID", script.data.script.script_id],
              ["Version", script.data.script.version],
              ["Parent script", script.data.script.parent_script_id],
              ["Row version", script.data.script.row_version],
              ["Status", script.data.script.status],
            ]}
          />
        </>
      )}

      <ConfirmInvalidationDialog
        open={pending !== null}
        title="Save this script change?"
        description={
          "A material change creates a new script version and marks the narration, storyboard, " +
          "shots and render stale. The current version is preserved, and nothing is regenerated " +
          "until you ask for it."
        }
        invalidation={null}
        confirmLabel="Save the beat"
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
