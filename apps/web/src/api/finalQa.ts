import type {
  FinalCompletionGateProjection,
  FinalEditorialCancelRequest,
  FinalEditorialCollectionResponse,
  FinalEditorialRemediationRequest,
  FinalEditorialRemediationResponse,
  FinalEditorialReviewRequest,
  FinalEditorialReviewResponse,
  FinalEditorialRunDetailProjection,
  FinalEditorialRunRequest,
  FinalEditorialRunResponse,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export function getProjectFinalQa(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<FinalEditorialCollectionResponse>> {
  return client.get<FinalEditorialCollectionResponse>(
    `/api/v1/projects/${projectId}/final-qa`,
    signal ? { signal } : {},
  );
}

export function getFinalQaRun(
  projectId: string,
  runId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<FinalEditorialRunDetailProjection>> {
  return client.get<FinalEditorialRunDetailProjection>(
    `/api/v1/projects/${projectId}/final-qa/${runId}`,
    signal ? { signal } : {},
  );
}

/**
 * The completion gate as the backend computes it. The dashboard renders this
 * answer; it never derives completion from the findings it happens to have.
 */
export function getFinalQaGate(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<FinalCompletionGateProjection>> {
  return client.get<FinalCompletionGateProjection>(
    `/api/v1/projects/${projectId}/final-qa/gate`,
    signal ? { signal } : {},
  );
}

export function startFinalQa(
  projectId: string,
  body: FinalEditorialRunRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<FinalEditorialRunResponse>> {
  return client.post<FinalEditorialRunResponse>(
    `/api/v1/projects/${projectId}/final-qa:run`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

export function cancelFinalQa(
  projectId: string,
  runId: string,
  body: FinalEditorialCancelRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<FinalEditorialRunResponse>> {
  return client.post<FinalEditorialRunResponse>(
    `/api/v1/projects/${projectId}/final-qa/${runId}:cancel`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

export function resolveFinalQaReview(
  projectId: string,
  runId: string,
  body: FinalEditorialReviewRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<FinalEditorialReviewResponse>> {
  return client.post<FinalEditorialReviewResponse>(
    `/api/v1/projects/${projectId}/final-qa/${runId}:review`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

export function routeFinalQaRemediation(
  projectId: string,
  runId: string,
  body: FinalEditorialRemediationRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<FinalEditorialRemediationResponse>> {
  return client.post<FinalEditorialRemediationResponse>(
    `/api/v1/projects/${projectId}/final-qa/${runId}:remediate`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}
