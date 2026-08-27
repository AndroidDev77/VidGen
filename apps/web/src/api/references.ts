import type { ApiResponse, VidGenClient } from "./client";

export interface ReferenceVersion {
  readonly id: string;
  readonly version: number;
  readonly status: "draft" | "approved" | "rejected" | "stale";
  readonly identity: Readonly<Record<string, unknown>>;
}

export interface ReferenceOverview {
  readonly project_id: string;
  readonly characters: readonly ReferenceVersion[];
  readonly locations: readonly ReferenceVersion[];
  readonly bindings: readonly Readonly<Record<string, unknown>>[];
}

export function getReferences(
  projectId: string,
  client: VidGenClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ReferenceOverview>> {
  return client.request(`/projects/${projectId}/references`, { signal });
}
