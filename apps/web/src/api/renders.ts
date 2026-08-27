import type { RenderProjection } from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export function getRender(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<RenderProjection>> {
  return client.get<RenderProjection>(
    `/api/v1/projects/${projectId}/render`,
    signal ? { signal } : {},
  );
}

export function startRender(
  projectId: string,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<{ render: RenderProjection }>> {
  return client.post<{ render: RenderProjection }>(
    `/api/v1/projects/${projectId}/render:start`,
    { body: { confirm_invalidation: true }, idempotencyKey },
  );
}
