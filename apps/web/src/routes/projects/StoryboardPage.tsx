import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageBar, MessageBarBody, MessageBarTitle, makeStyles, tokens } from "@fluentui/react-components";
import type { InvalidationSet } from "@vidgen/contracts";
import { useEffect, useState, type JSX } from "react";
import { useSearchParams } from "react-router-dom";

import { newIdempotencyKey } from "../../api/client";
import { queryKeys } from "../../api/queryKeys";
import {
  cancelShot,
  getShot,
  regenerateShot,
  retryShot,
  selectShotAttempt,
} from "../../api/shots";
import { getStoryboard } from "../../api/storyboards";
import { getDownloadUrl } from "../../api/uploads";
import { useApiClient } from "../../app/apiContext";
import { ConfirmInvalidationDialog } from "../../components/ConfirmInvalidationDialog";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { ShotInspector } from "../../components/ShotInspector";
import { StoryboardGrid } from "../../components/StoryboardGrid";
import { TimelinePreview } from "../../components/TimelinePreview";
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
    </div>
  );
}
