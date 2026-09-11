import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
} from "@fluentui/react-components";
import type { InvalidationSet, ScriptSegmentProjection } from "@vidgen/contracts";
import { useEffect, useState, type JSX } from "react";

import { newIdempotencyKey } from "../../api/client";
import { continueWorkflow } from "../../api/commands";
import { VidGenApiError } from "../../api/errors";
import { queryKeys } from "../../api/queryKeys";
import {
  getScript,
  listScripts,
  rejectScript,
  selectScript,
  updateScriptSegment,
} from "../../api/scripts";
import { useApiClient } from "../../app/apiContext";
import { ConfirmInvalidationDialog } from "../../components/ConfirmInvalidationDialog";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { ScriptCandidateList } from "../../components/ScriptCandidateList";
import { ScriptEditor, type ScriptDraft } from "../../components/ScriptEditor";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { PageStack } from "../../components/Surface";
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
      // Selecting a version is what clears `script_review_required`, so the
      // workflow header and this page's fallback both have to re-read it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(projectId) });
    },
  });

  const reject = useMutation({
    mutationFn: async ({ scriptId, reason }: { scriptId: string; reason: string }) => {
      const version = versions.data?.items.find((item) => item.script_id === scriptId);
      const rejected = await rejectScript(
        projectId,
        scriptId,
        reason,
        version?.row_version ?? 1,
        newIdempotencyKey(`script-reject-${scriptId}`),
        client,
      );
      // Rejecting only records the feedback. The next editing pass is the
      // paid step, and continuing the workflow from script generation is what
      // resumes the paused run with that feedback.
      await continueWorkflow(
        projectId,
        { entry_stage: "script_generation", reason: "review_resolved" },
        newIdempotencyKey(`script-revise-${scriptId}`),
        client,
      );
      return rejected.data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.scripts(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(projectId) });
    },
  });

  const retryScript = useMutation({
    mutationFn: () =>
      continueWorkflow(
        projectId,
        { entry_stage: "script_generation", reason: "review_resolved" },
        newIdempotencyKey("script-retry"),
        client,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(projectId) });
    },
  });

  const status = workflow.data?.status ?? "";
  // The pipeline stops without selecting a version when it runs out of revision
  // attempts, so `GET /script` has nothing to answer with. That is a choice to
  // make, not a failure to report: fall back to the candidates from `/scripts`.
  const scriptMissing =
    script.isError && script.error instanceof VidGenApiError && script.error.status === 404;
  const scriptReviewRequired =
    !script.isSuccess &&
    !script.isPending &&
    (status === "script_review_required" || scriptMissing);
  // A pass waiting on the reviewer is the normal checkpoint; retrying from
  // scratch is only the answer when the run left nothing to decide on.
  const awaitingDecision =
    versions.data?.items.some((item) => item.status === "pending_review") ?? false;

  return (
    <PageStack>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data?.name ?? "Project"}
        pageTitle="Script"
        workflow={workflow.data}
        connectionLabel={connectionLabel}
      />

      {script.isPending && <LoadingState label="Loading the script" rows={5} />}
      {scriptReviewRequired && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Pick the script to build on</MessageBarTitle>
            {awaitingDecision
              ? "An editing pass is waiting for your review. Approve the version the rest of " +
                "the pipeline should build on, or send it back with feedback for the next pass."
              : "The automatic script generation stopped without approving a version. Choose " +
                "one of the scripts below, or retry generation for a fresh compression and " +
                "writing pass."}
          </MessageBarBody>
          {!awaitingDecision && (
            <MessageBarActions>
              <Button
                appearance="primary"
                disabled={retryScript.isPending}
                onClick={() => retryScript.mutate()}
              >
                {retryScript.isPending ? "Retrying…" : "Retry script generation"}
              </Button>
            </MessageBarActions>
          )}
        </MessageBar>
      )}
      {scriptReviewRequired && versions.isPending && (
        <LoadingState label="Loading the available scripts" rows={3} />
      )}
      {scriptReviewRequired && versions.isError && (
        <ErrorState
          error={versions.error}
          title="The available scripts could not be loaded"
          onRetry={() => void versions.refetch()}
        />
      )}
      {scriptReviewRequired && versions.isSuccess && (
        <ScriptCandidateList
          projectId={projectId}
          candidates={versions.data.items}
          selectingScriptId={select.isPending ? (select.variables ?? null) : null}
          onSelect={(scriptId) => select.mutate(scriptId)}
          rejectingScriptId={reject.isPending ? (reject.variables?.scriptId ?? null) : null}
          onReject={(scriptId, reason) => reject.mutate({ scriptId, reason })}
        />
      )}
      {script.isError && !scriptReviewRequired && (
        <ErrorState
          error={script.error}
          title="No approved script yet"
          onRetry={() => void script.refetch()}
        />
      )}
      {save.isError && <ErrorState error={save.error} />}
      {select.isError && <ErrorState error={select.error} />}
      {reject.isError && <ErrorState error={reject.error} />}

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
          {/*
            An edit is preserved as a new draft revision, and a version picked
            out of a stalled run starts as a draft too. Either way the draft is
            what the reviewer is reading, and approving it is what lets the paid
            stages downstream build on it.
          */}
          <MessageBar intent={script.data.approved ? "success" : "warning"}>
            <MessageBarBody>
              <MessageBarTitle>
                {script.data.approved ? "Approved" : "This version is not approved yet"}
              </MessageBarTitle>
              {script.data.approved
                ? `Version ${script.data.script.version} is the approved script the rest of the ` +
                  "pipeline builds on."
                : `Version ${script.data.script.version} is a draft. Approving it rebuilds the ` +
                  "narration and everything downstream from it."}
            </MessageBarBody>
            {!script.data.approved && (
              <MessageBarActions>
                <Button
                  appearance="primary"
                  disabled={select.isPending}
                  onClick={() => select.mutate(script.data.script.script_id)}
                >
                  {select.isPending ? "Approving…" : "Approve this script"}
                </Button>
              </MessageBarActions>
            )}
          </MessageBar>
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
    </PageStack>
  );
}
