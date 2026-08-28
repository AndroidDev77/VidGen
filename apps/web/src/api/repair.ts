import type {
  RepairAction,
  RepairActionResponse,
  RepairCollectionResponse,
  RepairRunDetailProjection,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export function getProjectRepairs(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<RepairCollectionResponse>> {
  return client.get<RepairCollectionResponse>(
    `/api/v1/projects/${projectId}/repairs`,
    signal ? { signal } : {},
  );
}

export function getShotRepairs(
  projectId: string,
  shotId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<RepairCollectionResponse>> {
  return client.get<RepairCollectionResponse>(
    `/api/v1/projects/${projectId}/shots/${shotId}/repairs`,
    signal ? { signal } : {},
  );
}

export function getRepairRun(
  projectId: string,
  shotId: string,
  repairRunId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<RepairRunDetailProjection>> {
  return client.get<RepairRunDetailProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}/repairs/${repairRunId}`,
    signal ? { signal } : {},
  );
}

/**
 * Act on a repair run.
 *
 * No action here can mark a hard-failing visual as passed: selection requires a
 * new valid T20 result, and only a repair attempt can produce one. `retry`
 * resumes a durable technical operation rather than starting a paid generation,
 * and `cancel` takes effect before the next paid attempt.
 */
export function actOnRepairRun(
  projectId: string,
  shotId: string,
  repairRunId: string,
  action: RepairAction,
  reason: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<RepairActionResponse>> {
  return client.post<RepairActionResponse>(
    `/api/v1/projects/${projectId}/shots/${shotId}/repairs/${repairRunId}:act`,
    { body: { action, reason }, ifMatch: rowVersion, idempotencyKey },
  );
}
