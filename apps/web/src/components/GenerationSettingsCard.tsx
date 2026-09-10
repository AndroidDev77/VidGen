import { Button, Caption1, makeStyles, tokens } from "@fluentui/react-components";
import { VideoClipRegular } from "@fluentui/react-icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type JSX } from "react";

import {
  getGenerationSettings,
  setGenerationSettings,
  type GenerationSettingsInput,
} from "../api/projects";
import { queryKeys } from "../api/queryKeys";
import { useApiClient } from "../app/apiContext";
import { GenerationSettingsPanel } from "./GenerationSettingsPanel";
import { SectionCard } from "./Surface";
import { ErrorState, LoadingState } from "./states";

const useStyles = makeStyles({
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  muted: { color: tokens.colorNeutralForeground3 },
});

export interface GenerationSettingsCardProps {
  readonly projectId: string;
}

/** Edit an existing project's quality mode and pacing, with the estimate in view. */
export function GenerationSettingsCard({ projectId }: GenerationSettingsCardProps): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: queryKeys.generationSettings(projectId),
    queryFn: ({ signal }) =>
      getGenerationSettings(projectId, client, signal).then((response) => response.data),
    enabled: projectId !== "",
  });
  const [draft, setDraft] = useState<GenerationSettingsInput | null>(null);
  useEffect(() => {
    if (settings.data !== undefined) {
      setDraft({
        generation_quality: settings.data.settings.generation_quality,
        shot_pacing: settings.data.settings.shot_pacing,
        premium_fallback_allowed: settings.data.settings.premium_fallback_allowed,
        scene_detection_threshold:
          settings.data.settings.scene_detection_threshold ??
          settings.data.effective_scene_detection_threshold,
      });
    }
  }, [settings.data]);
  const save = useMutation({
    mutationFn: (input: GenerationSettingsInput) =>
      setGenerationSettings(projectId, input, client).then((response) => response.data),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.generationSettings(projectId), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });

  const dirty =
    settings.data !== undefined &&
    draft !== null &&
    (draft.generation_quality !== settings.data.settings.generation_quality ||
      draft.shot_pacing !== settings.data.settings.shot_pacing ||
      draft.premium_fallback_allowed !== settings.data.settings.premium_fallback_allowed ||
      draft.scene_detection_threshold !==
        (settings.data.settings.scene_detection_threshold ??
          settings.data.effective_scene_detection_threshold));

  return (
    <SectionCard
      title="Generation settings"
      icon={<VideoClipRegular />}
      description="Economy uses Gen-4 Turbo everywhere; balanced adds Gen-4.5 for hero shots; premium uses Gen-4.5 wherever compatible."
    >
      {settings.isPending && <LoadingState label="Loading generation settings" rows={2} />}
      {settings.isError && (
        <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />
      )}
      {settings.isSuccess && settings.data.settings !== undefined && draft !== null && (
        <>
          <GenerationSettingsPanel
            value={draft}
            onChange={setDraft}
            targetDurationSeconds={settings.data.estimate.target_duration_seconds}
            estimate={
              draft.shot_pacing === settings.data.settings.shot_pacing
                ? settings.data.estimate
                : undefined
            }
          />
          {settings.data.workflow_started && (
            <Caption1 className={styles.muted} role="note">
              A workflow has already started with the current settings. A change applies to
              the next generation run; shots already planned or animated under the current
              settings are never silently reused.
            </Caption1>
          )}
          <div className={styles.actions}>
            <Button
              appearance="primary"
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate(draft)}
              data-testid="save-generation-settings"
            >
              {save.isPending ? "Saving…" : "Save generation settings"}
            </Button>
          </div>
          {save.isError && <ErrorState error={save.error} />}
        </>
      )}
    </SectionCard>
  );
}
