import type { RenderApprovalProjection, RenderProjection } from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export interface ApproveRenderResult {
  approval: RenderApprovalProjection;
  render: RenderProjection;
}

export function approveRender(
  projectId: string,
  lineageHash: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ApproveRenderResult>> {
  return client.post<ApproveRenderResult>(`/api/v1/projects/${projectId}/review:approve`, {
    body: { lineage_hash: lineageHash },
    ifMatch: rowVersion,
    idempotencyKey,
  });
}
