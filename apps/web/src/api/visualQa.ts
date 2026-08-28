import type {
  VisualQACollectionResponse,
  VisualQADecisionResponse,
  VisualQAEvidenceResponse,
  VisualQARunDetailProjection,
  VisualQARunResponse,
  VisualQATargetType,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export function getProjectVisualQa(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<VisualQACollectionResponse>> {
  return client.get<VisualQACollectionResponse>(
    `/api/v1/projects/${projectId}/visual-qa`,
    signal ? { signal } : {},
  );
}

export function getShotVisualQa(
  projectId: string,
  shotId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<VisualQACollectionResponse>> {
  return client.get<VisualQACollectionResponse>(
    `/api/v1/projects/${projectId}/shots/${shotId}/visual-qa`,
    signal ? { signal } : {},
  );
}

export function getVisualQaRun(
  projectId: string,
  shotId: string,
  qaRunId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<VisualQARunDetailProjection>> {
  return client.get<VisualQARunDetailProjection>(
    `/api/v1/projects/${projectId}/shots/${shotId}/visual-qa/${qaRunId}`,
    signal ? { signal } : {},
  );
}

export function getVisualQaEvidence(
  projectId: string,
  shotId: string,
  qaRunId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<VisualQAEvidenceResponse>> {
  return client.get<VisualQAEvidenceResponse>(
    `/api/v1/projects/${projectId}/shots/${shotId}/visual-qa/${qaRunId}/evidence`,
    signal ? { signal } : {},
  );
}

export function runShotVisualQa(
  projectId: string,
  shotId: string,
  rowVersion: number,
  idempotencyKey: string,
  targets: readonly VisualQATargetType[],
  client: VidGenClient = apiClient,
): Promise<ApiResponse<VisualQARunResponse>> {
  return client.post<VisualQARunResponse>(
    `/api/v1/projects/${projectId}/shots/${shotId}/visual-qa:run`,
    { body: { provider: "fake", targets }, ifMatch: rowVersion, idempotencyKey },
  );
}

/**
 * Resolve an ambiguous `REVIEW` outcome. A hard failure can never be cleared
 * this way: the API rejects the attempt.
 */
export function decideVisualQa(
  projectId: string,
  shotId: string,
  qaRunId: string,
  decision: "approve" | "reject",
  reason: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<VisualQADecisionResponse>> {
  return client.post<VisualQADecisionResponse>(
    `/api/v1/projects/${projectId}/shots/${shotId}/visual-qa/${qaRunId}:${decision}`,
    { body: { reason }, ifMatch: rowVersion, idempotencyKey },
  );
}
