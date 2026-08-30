import type { ProjectCostSummaryResponse } from "@vidgen/contracts";

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
}

export interface ProjectStatus {
  project_id: string;
  status: string;
  source_video_id: string | null;
  source_asset_id: string | null;
  upload_status: string | null;
  error_code: string | null;
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
