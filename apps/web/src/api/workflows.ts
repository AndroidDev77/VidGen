import type { WorkflowStatusProjection } from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export interface StartWorkflowResult {
  workflow_id: string;
  run_id: string;
  status: WorkflowStatusProjection;
}

export function getWorkflow(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<WorkflowStatusProjection>> {
  return client.get<WorkflowStatusProjection>(
    `/api/v1/projects/${projectId}/workflow`,
    signal ? { signal } : {},
  );
}

export function startWorkflow(
  projectId: string,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
  subtitleAssetIds?: string[],
): Promise<ApiResponse<StartWorkflowResult>> {
  return client.post<StartWorkflowResult>(`/api/v1/projects/${projectId}/workflow:start`, {
    body: subtitleAssetIds?.length ? { subtitle_asset_ids: subtitleAssetIds } : {},
    idempotencyKey,
  });
}

export interface SubtitleUploadResult {
  asset_id: string;
}

export function uploadSubtitle(
  projectId: string,
  file: File,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<SubtitleUploadResult>> {
  return client.post<SubtitleUploadResult>(
    `/api/v1/projects/${projectId}/subtitle-uploads`,
    {
      rawBody: file,
      contentType: "application/x-subrip",
    },
  );
}

export function cancelWorkflow(
  projectId: string,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<WorkflowStatusProjection>> {
  return client.post<WorkflowStatusProjection>(
    `/api/v1/projects/${projectId}/workflow:cancel`,
    { idempotencyKey },
  );
}
