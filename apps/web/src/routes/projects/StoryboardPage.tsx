import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageBar, MessageBarBody, MessageBarTitle, makeStyles, tokens } from "@fluentui/react-components";
import type { InvalidationSet, RepairAction, VisualQARunProjection } from "@vidgen/contracts";
import { useCallback, useEffect, useMemo, useState, type JSX } from "react";
import { useSearchParams } from "react-router-dom";

import { newIdempotencyKey } from "../../api/client";
import { queryKeys } from "../../api/queryKeys";
import { actOnRepairRun, getRepairRun, getShotRepairs } from "../../api/repair";
import {
  cancelShot,
  getShot,
  regenerateShot,
  retryShot,
  selectShotAttempt,
} from "../../api/shots";
import { getStoryboard } from "../../api/storyboards";
import { getDownloadUrl } from "../../api/uploads";
import {
  decideVisualQa,
  getProjectVisualQa,
  getShotVisualQa,
  getVisualQaEvidence,
  getVisualQaRun,
  runShotVisualQa,
} from "../../api/visualQa";
import { useApiClient } from "../../app/apiContext";
import { ConfirmInvalidationDialog } from "../../components/ConfirmInvalidationDialog";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { RepairLineagePanel } from "../../components/RepairLineagePanel";
import { ShotInspector } from "../../components/ShotInspector";
import { StoryboardGrid } from "../../components/StoryboardGrid";
import { TimelinePreview } from "../../components/TimelinePreview";
import { VisualQAResultPanel } from "../../components/VisualQAResultPanel";
import { VisualQAReviewDialog } from "../../components/VisualQAReviewDialog";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { useProjectContext } from "./useProjectContext";

const useStyles = makeStyles({
  layout: {
    display: "grid",
    gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)",
    gap: tokens.spacingHorizontalXL,
    alignItems: "start",
    "@media (max-width: 1100px)": { gridTemplateColumns: "minmax(0, 1fr)" },
  },
  panel: {
    padding: tokens.spacingVerticalL,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
  },
});

type PendingAction = "regenerate" | null;

export function StoryboardPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const queryClient = useQueryClient();
  const { projectId, project, workflow, connectionLabel } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedShotId = searchParams.get("shot");
  const [pending, setPending] = useState<PendingAction>(null);
  const [invalidation, setInvalidation] = useState<InvalidationSet | null>(null);
  const [previewUrls, setPreviewUrls] = useState<ReadonlyMap<string, string>>(new Map());
  const [selectedQaRunId, setSelectedQaRunId] = useState<string | null>(null);
  const [qaDecision, setQaDecision] = useState<"approve" | "reject" | null>(null);
  const [selectedRepairRunId, setSelectedRepairRunId] = useState<string | null>(null);

  const storyboard = useQuery({
    queryKey: queryKeys.storyboard(projectId),
    queryFn: ({ signal }) => getStoryboard(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });

  const shot = useQuery({
    queryKey: queryKeys.shot(projectId, selectedShotId ?? ""),
    queryFn: ({ signal }) =>
      getShot(projectId, selectedShotId ?? "", client, signal).then((r) => r.data),
    enabled: projectId !== "" && selectedShotId !== null,
  });

  const visualQa = useQuery({
    queryKey: queryKeys.shotVisualQa(projectId, selectedShotId ?? ""),
    queryFn: ({ signal }) =>
      getShotVisualQa(projectId, selectedShotId ?? "", client, signal).then((r) => r.data),
    enabled: projectId !== "" && selectedShotId !== null,
  });

  const projectVisualQa = useQuery({
    queryKey: queryKeys.visualQa(projectId),
    queryFn: ({ signal }) =>
      getProjectVisualQa(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });

  const visualQaByShot = useMemo(() => {
    const byShot = new Map<string, VisualQARunProjection[]>();
    for (const run of projectVisualQa.data?.items ?? []) {
      byShot.set(run.shot_id, [...(byShot.get(run.shot_id) ?? []), run]);
    }
    return byShot;
  }, [projectVisualQa.data]);

  const qaRun = useQuery({
    queryKey: queryKeys.visualQaRun(projectId, selectedShotId ?? "", selectedQaRunId ?? ""),
    queryFn: ({ signal }) =>
      getVisualQaRun(projectId, selectedShotId ?? "", selectedQaRunId ?? "", client, signal).then(
        (r) => r.data,
      ),
    enabled: projectId !== "" && selectedShotId !== null && selectedQaRunId !== null,
  });

  const qaEvidence = useQuery({
    queryKey: queryKeys.visualQaEvidence(projectId, selectedShotId ?? "", selectedQaRunId ?? ""),
    queryFn: ({ signal }) =>
      getVisualQaEvidence(
        projectId,
        selectedShotId ?? "",
        selectedQaRunId ?? "",
        client,
        signal,
      ).then((r) => r.data),
    enabled: projectId !== "" && selectedShotId !== null && selectedQaRunId !== null,
  });

  // T21 repair visibility. A repair belongs to exactly one shot, so both
  // queries are shot-scoped and a repair action invalidates that shot only.
  const repairs = useQuery({
    queryKey: queryKeys.shotRepairs(projectId, selectedShotId ?? ""),
    queryFn: ({ signal }) =>
      getShotRepairs(projectId, selectedShotId ?? "", client, signal).then((r) => r.data),
    enabled: projectId !== "" && selectedShotId !== null,
  });

  const repairRun = useQuery({
    queryKey: queryKeys.repairRun(projectId, selectedShotId ?? "", selectedRepairRunId ?? ""),
    queryFn: ({ signal }) =>
      getRepairRun(projectId, selectedShotId ?? "", selectedRepairRunId ?? "", client, signal).then(
        (r) => r.data,
      ),
    enabled: projectId !== "" && selectedShotId !== null && selectedRepairRunId !== null,
  });

  const actOnRepair = useMutation({
    mutationFn: (action: RepairAction) =>
      actOnRepairRun(
        projectId,
        selectedShotId ?? "",
        selectedRepairRunId ?? "",
        action,
        "",
        repairRun.data?.row_version ?? shot.data?.shot.row_version ?? 1,
        newIdempotencyKey("repair-action"),
        client,
      ),
    onSuccess: () => {
      if (selectedShotId !== null) {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.shotRepairs(projectId, selectedShotId),
        });
        void queryClient.invalidateQueries({ queryKey: queryKeys.shot(projectId, selectedShotId) });
      }
    },
  });

  const invalidateVisualQa = (shotId: string) => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.shotVisualQa(projectId, shotId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.visualQa(projectId) });
  };

  const startVisualQa = useMutation({
    mutationFn: () =>
      runShotVisualQa(
        projectId,
        selectedShotId ?? "",
        shot.data?.shot.row_version ?? 1,
        newIdempotencyKey(`visual-qa-run-${selectedShotId ?? ""}`),
        ["keyframe", "video"],
        client,
      ),
    onSuccess: () => invalidateVisualQa(selectedShotId ?? ""),
  });

  const decideQa = useMutation({
    mutationFn: (input: { decision: "approve" | "reject"; reason: string }) =>
      decideVisualQa(
        projectId,
        selectedShotId ?? "",
        selectedQaRunId ?? "",
        input.decision,
        input.reason,
        shot.data?.shot.row_version ?? 1,
        newIdempotencyKey(`visual-qa-${input.decision}-${selectedQaRunId ?? ""}`),
        client,
      ),
    onSuccess: () => {
      setQaDecision(null);
      invalidateVisualQa(selectedShotId ?? "");
      invalidateShot(selectedShotId ?? "");
    },
    onError: () => setQaDecision(null),
  });

  // Evidence frames use the same short-lived signed URLs as the grid previews,
  // requested only when a frame is about to be displayed.
  // Memoised: the evidence viewer keys its fetch effect on this callback, so a
  // new identity on every render would re-request signed URLs forever.
  const resolveAssetUrl = useCallback(
    async (assetId: string): Promise<string | null> => {
      try {
        const { data } = await getDownloadUrl(assetId, client);
        return data.url;
      } catch {
        return null;
      }
    },
    [client],
  );

  // Keyframe previews use short-lived signed URLs, requested just before use
  // and held only in component state for this page view.
  useEffect(() => {
    const assets = (storyboard.data?.shots ?? [])
      .map((entry) => entry.selected_keyframe_asset_id)
      .filter((value): value is string => value !== null);
    if (assets.length === 0) {
      return;
    }
    let cancelled = false;
    void Promise.all(
      assets.map(async (assetId) => {
        try {
          const { data } = await getDownloadUrl(assetId, client);
          return [assetId, data.url] as const;
        } catch {
          return null;
        }
      }),
    ).then((results) => {
      if (!cancelled) {
        setPreviewUrls(new Map(results.filter((entry): entry is [string, string] => entry !== null)));
      }
    });
    return () => {
      cancelled = true;
      // Drop the signed URLs when they are no longer needed.
      setPreviewUrls(new Map());
    };
  }, [client, storyboard.data]);

  const invalidateShot = (shotId: string) => {
    // Only this shot's queries plus the storyboard summary; sibling shot
    // queries are deliberately left untouched.
    void queryClient.invalidateQueries({ queryKey: queryKeys.shot(projectId, shotId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.shotStatus(projectId, shotId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.storyboard(projectId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.render(projectId) });
  };

  const regenerate = useMutation({
    mutationFn: () =>
      regenerateShot(
        projectId,
        selectedShotId ?? "",
        shot.data?.shot.row_version ?? 1,
        newIdempotencyKey(`shot-regenerate-${selectedShotId ?? ""}`),
        true,
        client,
      ).then((r) => r.data),
    onSuccess: (result) => {
      setPending(null);
      setInvalidation(result.invalidation);
      invalidateShot(result.shot_id);
    },
    onError: () => setPending(null),
  });

  const retry = useMutation({
    mutationFn: () =>
      retryShot(
        projectId,
        selectedShotId ?? "",
        shot.data?.shot.row_version ?? 1,
        newIdempotencyKey(`shot-retry-${selectedShotId ?? ""}`),
        client,
      ),
    onSuccess: () => invalidateShot(selectedShotId ?? ""),
  });

  const cancelOne = useMutation({
    mutationFn: () =>
      cancelShot(
        projectId,
        selectedShotId ?? "",
        shot.data?.shot.row_version ?? 1,
        newIdempotencyKey(`shot-cancel-${selectedShotId ?? ""}`),
        client,
      ),
    onSuccess: () => invalidateShot(selectedShotId ?? ""),
  });

  const chooseAttempt = useMutation({
    mutationFn: (attemptId: string) =>
      selectShotAttempt(
        projectId,
        selectedShotId ?? "",
        attemptId,
        shot.data?.shot.row_version ?? 1,
        newIdempotencyKey(`shot-attempt-${attemptId}`),
        client,
      ),
    onSuccess: () => invalidateShot(selectedShotId ?? ""),
  });

  const selectShotId = (shotId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("shot", shotId);
    setSearchParams(next, { replace: false });
  };

  const busy =
    regenerate.isPending || retry.isPending || cancelOne.isPending || chooseAttempt.isPending;

  return (
    <div>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data?.name ?? "Project"}
        pageTitle="Storyboard"
        workflow={workflow.data}
        connectionLabel={connectionLabel}
      />

      {storyboard.isPending && <LoadingState label="Loading the storyboard" rows={4} />}
      {storyboard.isError && (
        <ErrorState
          error={storyboard.error}
          title="No storyboard yet"
          onRetry={() => void storyboard.refetch()}
        />
      )}

      {invalidation !== null && (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Regeneration started</MessageBarTitle>
            Marked stale: {invalidation.entries.map((entry) => entry.label).join(", ")}. Other
            shots keep their locked results.
          </MessageBarBody>
        </MessageBar>
      )}
      {regenerate.isError && <ErrorState error={regenerate.error} />}
      {retry.isError && <ErrorState error={retry.error} />}
      {cancelOne.isError && <ErrorState error={cancelOne.error} />}
      {chooseAttempt.isError && <ErrorState error={chooseAttempt.error} />}

      {storyboard.isSuccess && storyboard.data.shots.length === 0 && (
        <EmptyState
          title="No shots yet"
          description="The storyboard has not produced any canonical shots for this project."
        />
      )}

      {storyboard.isSuccess && storyboard.data.shots.length > 0 && (
        <div className={styles.layout}>
          <div>
            <TimelinePreview
              shots={storyboard.data.shots}
              totalDurationUs={storyboard.data.total_duration_us}
              selectedShotId={selectedShotId}
              onSelect={selectShotId}
            />
            <StoryboardGrid
              shots={storyboard.data.shots}
              selectedShotId={selectedShotId}
              onSelect={selectShotId}
              previewUrls={previewUrls}
              visualQaByShot={visualQaByShot}
            />
          </div>
          <aside className={styles.panel} aria-label="Shot inspector">
            {selectedShotId === null && (
              <EmptyState
                title="No shot selected"
                description="Choose a shot from the grid or the timeline to inspect it."
              />
            )}
            {selectedShotId !== null && shot.isPending && (
              <LoadingState label="Loading the shot" rows={3} />
            )}
            {selectedShotId !== null && shot.isError && <ErrorState error={shot.error} />}
            {shot.isSuccess && (
              <ShotInspector
                detail={shot.data}
                busy={busy}
                onRegenerate={() => setPending("regenerate")}
                onRetry={() => retry.mutate()}
                onCancel={() => cancelOne.mutate()}
                onSelectAttempt={(attemptId) => chooseAttempt.mutate(attemptId)}
                onRefreshStatus={() => void shot.refetch()}
              />
            )}
            {shot.isSuccess && selectedShotId !== null && (
              <VisualQAResultPanel
                runs={visualQa.data?.items ?? []}
                selected={qaRun.data ?? null}
                evidence={qaEvidence.data?.items ?? []}
                evidenceSamples={qaEvidence.data?.samples ?? []}
                busy={busy || startVisualQa.isPending || decideQa.isPending}
                onSelectRun={setSelectedQaRunId}
                onRunQa={() => startVisualQa.mutate()}
                onApprove={() => setQaDecision("approve")}
                onReject={() => setQaDecision("reject")}
                resolveAssetUrl={resolveAssetUrl}
              />
            )}
            {shot.isSuccess && selectedShotId !== null && (
              <RepairLineagePanel
                runs={repairs.data?.items ?? []}
                selected={repairRun.data ?? null}
                busy={busy || actOnRepair.isPending}
                onSelectRun={setSelectedRepairRunId}
                onAct={(action) => actOnRepair.mutate(action)}
              />
            )}
          </aside>
        </div>
      )}

      <ConfirmInvalidationDialog
        open={pending === "regenerate"}
        title="Regenerate this shot?"
        description={
          "This starts a new child workflow for this shot only. Sibling shots keep their locked " +
          "results and are not rerun. The current verified render is marked stale but preserved."
        }
        invalidation={null}
        confirmLabel="Regenerate the shot"
        busy={regenerate.isPending}
        onCancel={() => setPending(null)}
        onConfirm={() => regenerate.mutate()}
      />

      <VisualQAReviewDialog
        open={qaDecision !== null}
        decision={qaDecision ?? "approve"}
        busy={decideQa.isPending}
        hardFailure={qaRun.data?.hard_failure ?? false}
        onCancel={() => setQaDecision(null)}
        onConfirm={(reason) =>
          decideQa.mutate({ decision: qaDecision ?? "approve", reason })
        }
      />
    </div>
  );
}
