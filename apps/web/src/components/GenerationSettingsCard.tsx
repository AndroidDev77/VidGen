import { Button, Caption1, makeStyles, tokens } from "@fluentui/react-components";
import { VideoClipRegular } from "@fluentui/react-icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type JSX } from "react";

import {
  getGenerationSettings,
  setGenerationSettings,
  type GenerationSettingsInput,
  type GenerationSettingsResponse,
  type VisualQAPassScores,
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

const VISUAL_QA_SCORE_KEYS = [
  "utility_pass_score",
  "normal_pass_score",
  "hero_pass_score",
  "targeted_repair_floor",
] as const satisfies ReadonlyArray<keyof VisualQAPassScores>;

/**
 * The pass scores to edit: the project's own override where it has one, and
 * otherwise the gate actually in effect, so the panel always shows the numbers
 * the pipeline is using rather than a blank field.
 */
function visualQaScores(data: GenerationSettingsResponse): VisualQAPassScores {
  const override = data.settings.visual_qa_thresholds;
  const effective = data.effective_visual_qa_thresholds;
  return {
    utility_pass_score: override?.utility_pass_score ?? effective.utility_pass_score,
    normal_pass_score: override?.normal_pass_score ?? effective.normal_pass_score,
    hero_pass_score: override?.hero_pass_score ?? effective.hero_pass_score,
    targeted_repair_floor: override?.targeted_repair_floor ?? effective.targeted_repair_floor,
  };
}

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
        // Defaulted to empty rather than assumed present: an older API build
        // that predates these fields must still render the card.
        warn_only_validation_codes:
          settings.data.settings.warn_only_validation_codes ??
          settings.data.effective_warn_only_validation_codes ??
          [],
        script_warn_only_validation_codes:
          settings.data.settings.script_warn_only_validation_codes ??
          settings.data.effective_script_warn_only_validation_codes ??
          [],
        storyboard_warn_only_validation_codes:
          settings.data.settings.storyboard_warn_only_validation_codes ??
          settings.data.effective_storyboard_warn_only_validation_codes ??
          [],
        narration_warn_only_quality_codes:
          settings.data.settings.narration_warn_only_quality_codes ??
          settings.data.effective_narration_warn_only_quality_codes ??
          [],
        visual_qa_warn_only_codes:
          settings.data.settings.visual_qa_warn_only_codes ??
          settings.data.effective_visual_qa_warn_only_codes ??
          [],
        visual_qa_thresholds: visualQaScores(settings.data),
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
          settings.data.effective_scene_detection_threshold) ||
      // Order-insensitive: the API stores the codes sorted, the dropdown emits
      // them in the order they were checked.
      [...draft.warn_only_validation_codes].sort().join(",") !==
        [
          ...(settings.data.settings.warn_only_validation_codes ??
            settings.data.effective_warn_only_validation_codes ??
            []),
        ]
          .sort()
          .join(",") ||
      [...draft.script_warn_only_validation_codes].sort().join(",") !==
        [
          ...(settings.data.settings.script_warn_only_validation_codes ??
            settings.data.effective_script_warn_only_validation_codes ??
            []),
        ]
          .sort()
          .join(",") ||
      [...draft.storyboard_warn_only_validation_codes].sort().join(",") !==
        [
          ...(settings.data.settings.storyboard_warn_only_validation_codes ??
            settings.data.effective_storyboard_warn_only_validation_codes ??
            []),
        ]
          .sort()
          .join(",") ||
      [...draft.narration_warn_only_quality_codes].sort().join(",") !==
        [
          ...(settings.data.settings.narration_warn_only_quality_codes ??
            settings.data.effective_narration_warn_only_quality_codes ??
            []),
        ]
          .sort()
          .join(",") ||
      [...draft.visual_qa_warn_only_codes].sort().join(",") !==
        [
          ...(settings.data.settings.visual_qa_warn_only_codes ??
            settings.data.effective_visual_qa_warn_only_codes ??
            []),
        ]
          .sort()
          .join(",") ||
      VISUAL_QA_SCORE_KEYS.some(
        (key) => draft.visual_qa_thresholds[key] !== visualQaScores(settings.data)[key],
      ));

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
