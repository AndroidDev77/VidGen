import type { ProjectEventProjection } from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export interface ProjectEventPage {
  items: ProjectEventProjection[];
  last_event_id: number;
}

/** The polling fallback used after repeated Server-Sent Events failures. */
export function pollEvents(
  projectId: string,
  lastEventId: number | null,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ProjectEventPage>> {
  return client.get<ProjectEventPage>(`/api/v1/projects/${projectId}/events`, {
    query: { poll: true, ...(lastEventId === null ? {} : { last_event_id: lastEventId }) },
    ...(signal ? { signal } : {}),
  });
}

export function eventStreamUrl(
  projectId: string,
  client: VidGenClient = apiClient,
): string {
  return client.streamUrl(`/api/v1/projects/${projectId}/events`);
}
