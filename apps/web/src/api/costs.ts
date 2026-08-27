import type {
  PipelineFailureListResponse,
  ProviderAttemptListResponse,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export { getCosts } from "./projects";

export function listProviderAttempts(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ProviderAttemptListResponse>> {
  return client.get<ProviderAttemptListResponse>(
    `/api/v1/projects/${projectId}/provider-attempts`,
    { query: { limit: 10 }, ...(signal ? { signal } : {}) },
  );
}

export function listFailures(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<PipelineFailureListResponse>> {
  return client.get<PipelineFailureListResponse>(
    `/api/v1/projects/${projectId}/failures`,
    { query: { limit: 10 }, ...(signal ? { signal } : {}) },
  );
}
