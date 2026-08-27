import type { StoryboardProjection } from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export function getStoryboard(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<StoryboardProjection>> {
  return client.get<StoryboardProjection>(
    `/api/v1/projects/${projectId}/storyboard`,
    signal ? { signal } : {},
  );
}
