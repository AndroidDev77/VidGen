import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Caption1,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useEffect, useState, type JSX } from "react";
import { useSearchParams } from "react-router-dom";

import { newIdempotencyKey } from "../../api/client";
import { DOWNLOAD_URL_GC_MS, DOWNLOAD_URL_STALE_MS, queryKeys } from "../../api/queryKeys";
import { getRender } from "../../api/renders";
import { approveRender } from "../../api/reviews";
import { getDownloadUrl } from "../../api/uploads";
import { useApiClient } from "../../app/apiContext";
import { ApprovalBar } from "../../components/ApprovalBar";
import { AssetDownloadMenu } from "../../components/AssetDownloadMenu";
import { CaptionControls } from "../../components/CaptionControls";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { VideoPreview } from "../../components/VideoPreview";
import { ErrorState, LoadingState } from "../../components/states";
import { formatMicroseconds, formatTimestamp, humanize } from "../../state/format";
import { useProjectContext } from "./useProjectContext";

const useStyles = makeStyles({
  layout: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalL },
  facts: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
    gap: tokens.spacingHorizontalM,
  },
  fact: { display: "flex", flexDirection: "column" },
});

export function FinalReviewPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const queryClient = useQueryClient();
  const { projectId, project, workflow, connectionLabel } = useProjectContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const captionsEnabled = searchParams.get("captions") !== "off";
  const [downloadError, setDownloadError] = useState<unknown>(null);

  const render = useQuery({
    queryKey: queryKeys.render(projectId),
    queryFn: ({ signal }) => getRender(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });

  const videoAssetId = render.data?.final_video_asset_id ?? null;
  const captionAssetId = render.data?.webvtt_asset_id ?? null;

  // Signed URLs are refreshed shortly before playback and expire quickly, so
  // they never become permanent application state.
  const videoUrl = useQuery({
    queryKey: queryKeys.downloadUrl(videoAssetId ?? ""),
    queryFn: ({ signal }) => getDownloadUrl(videoAssetId ?? "", client, signal).then((r) => r.data.url),
    enabled: videoAssetId !== null,
    staleTime: DOWNLOAD_URL_STALE_MS,
    gcTime: DOWNLOAD_URL_GC_MS,
    refetchInterval: DOWNLOAD_URL_STALE_MS,
  });
  const captionUrl = useQuery({
    queryKey: queryKeys.downloadUrl(captionAssetId ?? ""),
    queryFn: ({ signal }) =>
      getDownloadUrl(captionAssetId ?? "", client, signal).then((r) => r.data.url),
    enabled: captionAssetId !== null,
    staleTime: DOWNLOAD_URL_STALE_MS,
    gcTime: DOWNLOAD_URL_GC_MS,
  });

  // Drop cached signed URLs when leaving the page.
  useEffect(
    () => () => {
      queryClient.removeQueries({ queryKey: ["assets"] });
    },
    [queryClient],
  );

  const approve = useMutation({
    mutationFn: () => {
      const current = render.data;
      if (current === undefined || current.lineage_hash === null) {
        throw new Error("The render lineage is not available yet.");
      }
      return approveRender(
        projectId,
        current.lineage_hash,
        current.row_version,
        newIdempotencyKey(`approve-${current.render_job_id}`),
        client,
      ).then((r) => r.data);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.render(projectId) });
    },
  });

  const toggleCaptions = (enabled: boolean) => {
    const next = new URLSearchParams(searchParams);
    if (enabled) {
      next.delete("captions");
    } else {
      next.set("captions", "off");
    }
    setSearchParams(next, { replace: true });
  };

  return (
    <div>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data?.name ?? "Project"}
        pageTitle="Final review"
        workflow={workflow.data}
        connectionLabel={connectionLabel}
      />

      {render.isPending && <LoadingState label="Loading the final render" rows={4} />}
      {render.isError && (
        <ErrorState
          error={render.error}
          title="No completed render yet"
          onRetry={() => void render.refetch()}
        />
      )}
      {render.isSuccess && render.data.status !== "render_complete" && (
        <MessageBar intent="info">
          <MessageBarBody>
            <MessageBarTitle>The render is not finished</MessageBarTitle>
            Current status: {humanize(render.data.status)}. Preview and approval unlock once the
            render completes and verification succeeds.
          </MessageBarBody>
        </MessageBar>
      )}

      {render.isSuccess && render.data.status === "render_complete" && (
        <div className={styles.layout}>
          {render.data.stale && (
            <MessageBar intent="warning">
              <MessageBarBody>
                <MessageBarTitle>This render is stale</MessageBarTitle>
                Something it depends on changed after it was produced. It is preserved as a
                historical record; produce a new render for the current lineage.
              </MessageBarBody>
            </MessageBar>
          )}

          <VideoPreview
            videoUrl={videoUrl.data ?? null}
            captionsUrl={captionUrl.data ?? null}
            captionsEnabled={captionsEnabled}
            captionLanguage={render.data.caption_language ?? "en"}
            title={project.data?.name ?? "the project"}
          />

          <CaptionControls
            enabled={captionsEnabled}
            onToggle={toggleCaptions}
            language={render.data.caption_language}
            cueCount={render.data.caption_cue_count}
            subtitleMode={render.data.subtitle_mode}
            disabled={captionAssetId === null}
          />

          <section aria-labelledby="render-facts-heading">
            <Subtitle2 as="h2" id="render-facts-heading">
              Render summary
            </Subtitle2>
            <div className={styles.facts}>
              <Fact label="Status" value={humanize(render.data.status)} />
              <Fact label="Render version" value={render.data.render_version} />
              <Fact
                label="Verification"
                value={render.data.verified ? "Passed" : "No successful report"}
              />
              <Fact
                label="Expected duration"
                value={formatMicroseconds(render.data.expected_duration_us)}
              />
              <Fact
                label="Measured duration"
                value={formatMicroseconds(render.data.measured_duration_us)}
              />
              <Fact label="Selected shots" value={String(render.data.selected_shot_count)} />
              <Fact
                label="Integrated loudness"
                value={
                  render.data.integrated_loudness_lufs === null
                    ? "—"
                    : `${render.data.integrated_loudness_lufs.toFixed(1)} LUFS`
                }
              />
              <Fact
                label="True peak"
                value={
                  render.data.true_peak_dbtp === null
                    ? "—"
                    : `${render.data.true_peak_dbtp.toFixed(1)} dBTP`
                }
              />
              <Fact
                label="Warnings"
                value={
                  render.data.warning_codes.length === 0
                    ? "None"
                    : render.data.warning_codes.map(humanize).join(", ")
                }
              />
              <Fact label="Completed" value={formatTimestamp(render.data.completed_at)} />
            </div>
          </section>

          {approve.isError && <ErrorState error={approve.error} />}
          {downloadError !== null && <ErrorState error={downloadError} />}

          <ApprovalBar
            render={render.data}
            busy={approve.isPending}
            onApprove={() => approve.mutate()}
          >
            <AssetDownloadMenu
              client={client}
              onError={setDownloadError}
              targets={[
                {
                  label: "Final MP4",
                  assetId: render.data.final_video_asset_id,
                  fileName: `${project.data?.name ?? "recap"}.mp4`,
                },
                {
                  label: "Captions (SRT)",
                  assetId: render.data.srt_asset_id,
                  fileName: `${project.data?.name ?? "recap"}.srt`,
                },
                {
                  label: "Captions (WebVTT)",
                  assetId: render.data.webvtt_asset_id,
                  fileName: `${project.data?.name ?? "recap"}.vtt`,
                },
              ]}
            />
          </ApprovalBar>

          <TechnicalDetails
            title="Render provenance"
            entries={[
              ["Render job", render.data.render_job_id],
              ["Render identity", render.data.render_identity],
              ["Lineage hash", render.data.lineage_hash],
              ["Script", render.data.script_id],
              ["Script version", render.data.script_version],
              ["Storyboard run", render.data.storyboard_run_id],
              ["Narration run", render.data.narration_run_id],
              ["Manifest asset", render.data.manifest_asset_id],
              ["Verification report", render.data.verification_report_asset_id],
              ["FFmpeg", render.data.ffmpeg_version],
              ["Row version", render.data.row_version],
            ]}
          />
        </div>
      )}
    </div>
  );
}

function Fact({ label, value }: { readonly label: string; readonly value: string }): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.fact}>
      <Caption1>{label}</Caption1>
      <Subtitle2>{value}</Subtitle2>
    </div>
  );
}
