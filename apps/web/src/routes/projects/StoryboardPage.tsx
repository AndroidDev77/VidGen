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
import { StoryboardReviewBar } from "../../components/StoryboardReviewBar";
import { TimelinePreview } from "../../components/TimelinePreview";
import { VisualQAResultPanel } from "../../components/VisualQAResultPanel";
import { VisualQAReviewDialog } from "../../components/VisualQAReviewDialog";
import { PageStack } from "../../components/Surface";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { shotCommandStateFor } from "../../state/shotCommand";
import { decidableRun, decisionAffordance, type QaDecisionAffordance } from "../../state/visualQa";
import { useProjectContext } from "./useProjectContext";

const useStyles = makeStyles({
  layout: {
    display: "grid",
    gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)",
    gap: tokens.spacingHorizontalXL,
    alignItems: "start",
    "@media (max-width: 1100px)": { gridTemplateColumns: "minmax(0, 1fr)" },
  },
  column: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalL, minWidth: 0 },
  panel: {
    padding: tokens.spacingVerticalL,
    borderRadius: tokens.borderRadiusLarge,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    boxShadow: tokens.shadow2,
    // Long inspectors should scroll within the sticky column, not push the
    // whole page down past the shot grid.
    position: "sticky",
    top: tokens.spacingVerticalXXL,
    maxHeight: "calc(100vh - 120px)",
    overflowY: "auto",
  },
});

type PendingAction = "regenerate" | null;

/** One shot's decidable QA run, with everything the decision call needs. */
interface QaDecisionTarget {
  readonly shotId: string;
  readonly qaRunId: string;
  readonly rowVersion: number;
  readonly affordance: Exclude<QaDecisionAffordance, null>;
  /** Carried so the confirmation dialog can refuse an unclearable result. */
  readonly hardFailure: boolean;
}

/** A decision awaiting its reason. One shot from a card, many from the bar. */
interface PendingQaDecision {
  readonly decision: "approve" | "reject";
  readonly targets: readonly QaDecisionTarget[];
}

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
  const [selectedVideoUrl, setSelectedVideoUrl] = useState<string | null>(null);
  const [selectedQaRunId, setSelectedQaRunId] = useState<string | null>(null);
  const [qaDecision, setQaDecision] = useState<PendingQaDecision | null>(null);
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

  // Every shot whose QA is still waiting on a person, in storyboard order.
  // Built from the storyboard rather than from the QA collection so the bar's
  // counts, the card buttons and the bulk actions always name the same shots.
  const decisionTargets = useMemo(() => {
    const targets: QaDecisionTarget[] = [];
    for (const entry of storyboard.data?.shots ?? []) {
      // A shot whose decision is already queued is not waiting on a person any
      // more, whatever its QA run still says. Leaving it in would let the bulk
      // actions - and the review counts above them - fire a second command for
      // work the dispatcher has not got to yet. A command parked *on* a human
      // decision is the opposite case and stays in: it is released by the very
      // decision these actions make.
      if (shotCommandStateFor(entry).inFlight) {
        continue;
      }
      const run = decidableRun(visualQaByShot.get(entry.shot_id) ?? []);
      const affordance = decisionAffordance(run);
      if (run !== null && affordance !== null) {
        targets.push({
          shotId: entry.shot_id,
          qaRunId: run.qa_run_id,
          rowVersion: entry.row_version,
          affordance,
          hardFailure: run.hard_failure,
        });
      }
    }
    return targets;
  }, [storyboard.data, visualQaByShot]);

  const reviewCounts = useMemo(
    () => ({
      review: decisionTargets.filter((target) => target.affordance === "review").length,
      failed: decisionTargets.filter((target) => target.affordance === "override").length,
    }),
    [decisionTargets],
  );

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
        // The detail query carries the row version the next action echoes back
        // in If-Match, so a stale one would make a second action conflict.
        if (selectedRepairRunId !== null) {
          void queryClient.invalidateQueries({
            queryKey: queryKeys.repairRun(projectId, selectedShotId, selectedRepairRunId),
          });
        }
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
    mutationFn: async (input: { pending: PendingQaDecision; reason: string }) => {
      // One request per shot, in order. A decision bumps that shot's row
      // version, so batching them into one call would need an endpoint that
      // does not exist - and a partial failure here still leaves every shot it
      // did reach correctly decided.
      for (const target of input.pending.targets) {
        await decideVisualQa(
          projectId,
          target.shotId,
          target.qaRunId,
          input.pending.decision,
          input.reason,
          target.rowVersion,
          newIdempotencyKey(`visual-qa-${input.pending.decision}-${target.qaRunId}`),
          client,
        );
      }
      return input.pending.targets.map((target) => target.shotId);
    },
    onSettled: (shotIds) => {
      setQaDecision(null);
      // Even a failed batch may have decided some shots before it stopped, so
      // the refetch happens either way rather than only on success.
      void queryClient.invalidateQueries({ queryKey: queryKeys.visualQa(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.storyboard(projectId) });
      for (const shotId of shotIds ?? []) {
        invalidateVisualQa(shotId);
        invalidateShot(shotId);
      }
    },
  });

  /** The decision a shot card offers: the run that shot is actually waiting on. */
  const cardTarget = useCallback(
    (shotId: string): QaDecisionTarget | null =>
      decisionTargets.find((target) => target.shotId === shotId) ?? null,
    [decisionTargets],
  );

  const decideOneShot = useCallback(
    (shotId: string, decision: "approve" | "reject") => {
      const target = cardTarget(shotId);
      if (target !== null) {
        setQaDecision({ decision, targets: [target] });
      }
    },
    [cardTarget],
  );

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

  // Fetch a signed video URL whenever the selected shot changes.
  useEffect(() => {
    const videoAssetId = shot.data?.shot.selected_video_asset_id ?? null;
    if (!videoAssetId) {
      setSelectedVideoUrl(null);
      return;
    }
    let cancelled = false;
    void getDownloadUrl(videoAssetId, client).then(({ data }) => {
      if (!cancelled) setSelectedVideoUrl(data.url);
    }).catch(() => {
      if (!cancelled) setSelectedVideoUrl(null);
    });
    return () => { cancelled = true; setSelectedVideoUrl(null); };
  }, [client, shot.data?.shot.selected_video_asset_id]);

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

  // A command already in flight against the selected shot locks the same
  // actions the mutations above do: both would enqueue duplicate work.
  const selectedCommandInFlight =
    shot.data !== undefined && shotCommandStateFor(shot.data.shot).inFlight;
  const busy =
    regenerate.isPending || retry.isPending || cancelOne.isPending || chooseAttempt.isPending;
  // The QA run the inspector has open, which is not always the run the shot is
  // waiting on: a reviewer may be inspecting the keyframe result of a shot
  // whose video result is the decidable one.
  const inspectorTarget: QaDecisionTarget | null = (() => {
    const run = qaRun.data ?? null;
    const affordance = decisionAffordance(run);
    if (run === null || affordance === null || selectedShotId === null) {
      return null;
    }
    return {
      shotId: selectedShotId,
      qaRunId: run.qa_run_id,
      rowVersion: shot.data?.shot.row_version ?? 1,
      affordance,
      hardFailure: run.hard_failure,
    };
  })();
  const decideSelectedRun = (decision: "approve" | "reject") => {
    if (inspectorTarget !== null) {
      setQaDecision({ decision, targets: [inspectorTarget] });
    }
  };

  return (
    <PageStack>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data?.name ?? "Project"}
        pageTitle="Storyboard"
        workflow={workflow.data}
        connectionLabel={connectionLabel}
        reviewPrompt={reviewCounts}
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
      {decideQa.isError && <ErrorState error={decideQa.error} />}

      {storyboard.isSuccess && storyboard.data.shots.length === 0 && (
        <EmptyState
          title="No shots yet"
          description="The storyboard has not produced any canonical shots for this project."
        />
      )}

      {storyboard.isSuccess && storyboard.data.shots.length > 0 && (
        <div className={styles.layout}>
          <div className={styles.column}>
            <TimelinePreview
              shots={storyboard.data.shots}
              totalDurationUs={storyboard.data.total_duration_us}
              selectedShotId={selectedShotId}
              onSelect={selectShotId}
            />
            <StoryboardReviewBar
              counts={reviewCounts}
              busy={decideQa.isPending}
              onApproveReviewable={() =>
                setQaDecision({
                  decision: "approve",
                  targets: decisionTargets.filter((target) => target.affordance === "review"),
                })
              }
              onForceApproveFailed={() =>
                setQaDecision({
                  decision: "approve",
                  targets: decisionTargets.filter((target) => target.affordance === "override"),
                })
              }
            />
            <StoryboardGrid
              shots={storyboard.data.shots}
              selectedShotId={selectedShotId}
              onSelect={selectShotId}
              previewUrls={previewUrls}
              visualQaByShot={visualQaByShot}
              busy={decideQa.isPending}
              onApprove={(shotId) => decideOneShot(shotId, "approve")}
              onReject={(shotId) => decideOneShot(shotId, "reject")}
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
                videoUrl={selectedVideoUrl}
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
                busy={
                  busy ||
                  selectedCommandInFlight ||
                  startVisualQa.isPending ||
                  decideQa.isPending
                }
                onSelectRun={setSelectedQaRunId}
                onRunQa={() => startVisualQa.mutate()}
                onApprove={() => decideSelectedRun("approve")}
                onReject={() => decideSelectedRun("reject")}
                resolveAssetUrl={resolveAssetUrl}
              />
            )}
            {shot.isSuccess && selectedShotId !== null && (
              <RepairLineagePanel
                runs={repairs.data?.items ?? []}
                selected={repairRun.data ?? null}
                busy={busy || selectedCommandInFlight || actOnRepair.isPending}
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
        decision={qaDecision?.decision ?? "approve"}
        busy={decideQa.isPending}
        hardFailure={qaDecision?.targets.some((target) => target.hardFailure) ?? false}
        shotCount={qaDecision?.targets.length ?? 1}
        overriding={qaDecision?.targets.some((target) => target.affordance === "override") ?? false}
        onCancel={() => setQaDecision(null)}
        onConfirm={(reason) => {
          if (qaDecision !== null) {
            decideQa.mutate({ pending: qaDecision, reason });
          }
        }}
      />
    </PageStack>
  );
}
