import type {
  ShotDetailProjection,
  ShotRegenerationResult,
  ShotStatusProjection,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export function getShot(
  projectId: string,
  shotId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ShotDetailProjection>> {
  return client.get<ShotDetailProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}`,
    signal ? { signal } : {},
  );
}

export function getShotStatus(
  projectId: string,
  shotId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ShotStatusProjection>> {
  return client.get<ShotStatusProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}/status`,
    signal ? { signal } : {},
  );
}

export function regenerateShot(
  projectId: string,
  shotId: string,
  rowVersion: number,
  idempotencyKey: string,
  confirmInvalidation: boolean,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ShotRegenerationResult>> {
  return client.post<ShotRegenerationResult>(
    `/api/v1/projects/${projectId}/shots/${shotId}:regenerate`,
    {
      body: { confirm_invalidation: confirmInvalidation },
      ifMatch: rowVersion,
      idempotencyKey,
    },
  );
}

export function retryShot(
  projectId: string,
  shotId: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ShotStatusProjection>> {
  return client.post<ShotStatusProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}:retry`,
    { ifMatch: rowVersion, idempotencyKey },
  );
}

export function cancelShot(
  projectId: string,
  shotId: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ShotStatusProjection>> {
  return client.post<ShotStatusProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}:cancel`,
    { ifMatch: rowVersion, idempotencyKey },
  );
}

export function selectShotAttempt(
  projectId: string,
  shotId: string,
  attemptId: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ShotDetailProjection>> {
  return client.post<ShotDetailProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}:select-attempt`,
    { body: { attempt_id: attemptId }, ifMatch: rowVersion, idempotencyKey },
  );
}
