import type {
  GenerationCostEstimate,
  GenerationQuality,
  NarrationQualityThresholds,
  ProjectCostSummaryResponse,
  ProjectGenerationSettings,
  ShotPacing,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

/** The project-list row the API returns (`ProjectListItemResponse`). */
export interface ProjectListItem {
  id: string;
  name: string;
  status: string;
  target_duration_seconds: number;
  visual_style: string;
  humor_intensity: number;
  created_at: string;
  updated_at: string;
  current_stage: string | null;
  progress_percentage: number | null;
  committed_cost_amount: string | null;
  hard_cap_amount: string | null;
  has_failures: boolean;
  latest_failure_stage: string | null;
  latest_failure_code: string | null;
  row_version: number;
}

export interface ProjectDetail {
  id: string;
  name: string;
  status: string;
  target_duration_seconds: number;
  visual_style: string;
  humor_intensity: number;
  created_at: string;
  updated_at: string;
  /**
   * The project's narration voice. `null` means the workflow cannot start yet,
   * which the setup screen and the dashboard both say out loud rather than
   * letting the start button fail.
   */
  voice_profile_id: string | null;
  /** The resolved generation settings; legacy projects resolve deterministically. */
  generation_quality: GenerationQuality;
  shot_pacing: ShotPacing;
  premium_fallback_allowed: boolean;
}

/** The owner's choice of Runway model tier, shot pacing and scene sensitivity. */
export interface GenerationSettingsInput {
  generation_quality: GenerationQuality;
  shot_pacing: ShotPacing;
  premium_fallback_allowed: boolean;
  /** Scene-cut sensitivity for media processing (0.10-0.90, exclusive of 0 and 1). */
  scene_detection_threshold: number;
  /**
   * Episode-analysis validation codes reported as warnings instead of failing
   * the run. An empty list means every code fails the run.
   */
  warn_only_validation_codes: string[];
  /** The same, for T11 plot compression and script validation. */
  script_warn_only_validation_codes: string[];
  /** The same, for T13 storyboard validation. */
  storyboard_warn_only_validation_codes: string[];
}

export interface GenerationSettingsResponse {
  project_id: string;
  settings: ProjectGenerationSettings;
  generation_policy_identity: string;
  workflow_started: boolean;
  estimate: GenerationCostEstimate;
  /** The scene-cut sensitivity actually in effect: the override, or the deployment default. */
  effective_scene_detection_threshold: number;
  /** The validation codes actually demoted to warnings for this project. */
  effective_warn_only_validation_codes: string[];
  /** Every code a project may choose to treat as a warning. */
  available_warn_only_validation_codes: string[];
  /** The same two lists for the T11 compression validator. */
  effective_script_warn_only_validation_codes: string[];
  available_script_warn_only_validation_codes: string[];
  /** The T12 narration quality gate in effect after the project's overrides. */
  effective_narration_quality_thresholds: NarrationQualityThresholds;
  /** The narration quality codes demoted to warnings, and every code that may be. */
  effective_narration_warn_only_quality_codes: string[];
  available_narration_warn_only_quality_codes: string[];
  /** The same two lists for the T13 storyboard validator. */
  effective_storyboard_warn_only_validation_codes: string[];
  available_storyboard_warn_only_validation_codes: string[];
  /** The same two lists for the T20 visual-QA gate. */
  effective_visual_qa_warn_only_codes: string[];
  available_visual_qa_warn_only_codes: string[];
}

export interface GenerationEstimateInput {
  target_duration_seconds: number;
  shot_pacing: ShotPacing;
}

/** The generic lifecycle every stage's progress moves through. */
export type StageProgressState = "queued" | "running" | "waiting" | "completed" | "failed";

/**
 * Where the project's current stage is (`StageProgressResponse`).
 *
 * The backend derives this from each stage's durable checkpoints, so the
 * figures survive a worker restart and never run ahead of what has been
 * persisted. `waiting` means a human gate: nothing moves until the owner acts.
 */
export interface StageProgress {
  /** A stable stage id; the timeline's `PipelineStage` value where one exists. */
  stage: string;
  /** The heading to show, e.g. "Episode analysis" or "Quality review". */
  label: string;
  state: StageProgressState;
  /** The stage-specific phase, e.g. "scene_analysis" or "animating". */
  phase: string;
  completed_count: number;
  total_count: number;
  /** Singular noun for one counted unit, e.g. "scene". */
  unit: string;
  /** What the counts count, e.g. "scenes analyzed". */
  count_label: string;
  /** 0 to 100. */
  percentage: number;
  message: string;
  error_code: string | null;
  updated_at: string | null;
}

export interface ProjectStatus {
  project_id: string;
  status: string;
  source_video_id: string | null;
  source_asset_id: string | null;
  upload_status: string | null;
  error_code: string | null;
  /** The stage that moved most recently; `null` until any stage has started. */
  stage_progress: StageProgress | null;
}

export interface CreateProjectInput {
  name: string;
  target_duration_seconds: number;
  visual_style: string;
  humor_intensity: number;
  /** A voice from this deployment's catalog, chosen during setup. */
  voice_profile_id?: string;
  /**
   * The project's spend caps in USD, as exact decimal strings. Sending a number
   * would let a binary float round the limit the owner typed, so the form keeps
   * the text it collected.
   */
  budget_warning_cap: string;
  budget_hard_cap: string;
  /** Strict values: "economy" | "balanced" | "premium" and "relaxed" | "normal" | "fast". */
  generation_quality: GenerationQuality;
  shot_pacing: ShotPacing;
  premium_fallback_allowed: boolean;
  /** Scene-cut sensitivity for media processing (0.10-0.90, exclusive of 0 and 1). */
  scene_detection_threshold: number;
  /** Episode-analysis validation codes reported as warnings instead of errors. */
  warn_only_validation_codes: string[];
  /** The same, for T11 plot compression and script validation. */
  script_warn_only_validation_codes: string[];
  /** The same, for T13 storyboard validation. */
  storyboard_warn_only_validation_codes: string[];
}

export function listProjects(
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ProjectListItem[]>> {
  return client.get<ProjectListItem[]>("/api/v1/projects", signal ? { signal } : {});
}

export function getProject(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ProjectDetail>> {
  return client.get<ProjectDetail>(`/api/v1/projects/${projectId}`, signal ? { signal } : {});
}

export function getProjectStatus(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ProjectStatus>> {
  return client.get<ProjectStatus>(
    `/api/v1/projects/${projectId}/status`,
    signal ? { signal } : {},
  );
}

export function createProject(
  input: CreateProjectInput,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ProjectDetail>> {
  return client.post<ProjectDetail>("/api/v1/projects", { body: input });
}

export function deleteProject(
  projectId: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<void>> {
  return client.delete<void>(`/api/v1/projects/${projectId}`);
}

export function getCosts(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ProjectCostSummaryResponse>> {
  return client.get<ProjectCostSummaryResponse>(
    `/api/v1/projects/${projectId}/costs`,
    signal ? { signal } : {},
  );
}

export function getGenerationEstimate(
  input: GenerationEstimateInput,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<GenerationCostEstimate>> {
  return client.post<GenerationCostEstimate>("/api/v1/projects/generation-estimate", {
    body: input,
    ...(signal ? { signal } : {}),
  });
}

export function getGenerationSettings(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<GenerationSettingsResponse>> {
  return client.get<GenerationSettingsResponse>(
    `/api/v1/projects/${projectId}/generation-settings`,
    signal ? { signal } : {},
  );
}

export function setGenerationSettings(
  projectId: string,
  input: GenerationSettingsInput,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<GenerationSettingsResponse>> {
  return client.put<GenerationSettingsResponse>(
    `/api/v1/projects/${projectId}/generation-settings`,
    { body: input },
  );
}
