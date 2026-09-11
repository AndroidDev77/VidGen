import type {
  InvalidationSet,
  ScriptProjection,
  ScriptSegmentProjection,
  ScriptSummaryProjection,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export interface ScriptSegmentUpdate {
  text?: string;
  visual_gag?: string;
  confirm_invalidation: boolean;
}

export interface ScriptSegmentUpdateResult {
  segment: ScriptSegmentProjection;
  script: ScriptSummaryProjection;
  created_version: boolean;
  invalidation: InvalidationSet;
}

export function getScript(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ScriptProjection>> {
  return client.get<ScriptProjection>(
    `/api/v1/projects/${projectId}/script`,
    signal ? { signal } : {},
  );
}

export function listScripts(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<{ items: ScriptSummaryProjection[] }>> {
  return client.get<{ items: ScriptSummaryProjection[] }>(
    `/api/v1/projects/${projectId}/scripts`,
    signal ? { signal } : {},
  );
}

export function selectScript(
  projectId: string,
  scriptId: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<{ script: ScriptSummaryProjection }>> {
  return client.post<{ script: ScriptSummaryProjection }>(
    `/api/v1/projects/${projectId}/scripts/${scriptId}:select`,
    { ifMatch: rowVersion, idempotencyKey },
  );
}

export interface ScriptRejectionResult {
  script: ScriptSummaryProjection;
  editing_pass: number;
  max_editing_passes: number;
  passes_remaining: number;
}

/**
 * Reject an editing pass with the feedback the next pass must address.
 *
 * Rejecting records the reason on the version; the next pass only starts when
 * the workflow is continued from `script_generation`, which is a separate,
 * paid step the caller takes explicitly.
 */
export function rejectScript(
  projectId: string,
  scriptId: string,
  reason: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ScriptRejectionResult>> {
  return client.post<ScriptRejectionResult>(
    `/api/v1/projects/${projectId}/scripts/${scriptId}:reject`,
    { body: { reason }, ifMatch: rowVersion, idempotencyKey },
  );
}

export function updateScriptSegment(
  projectId: string,
  segmentId: string,
  update: ScriptSegmentUpdate,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ScriptSegmentUpdateResult>> {
  return client.patch<ScriptSegmentUpdateResult>(
    `/api/v1/projects/${projectId}/script-segments/${segmentId}`,
    { body: update, ifMatch: rowVersion, idempotencyKey },
  );
}

/**
 * Fetch one script version by ID.
 *
 * Used before a version is selected: `GET /script` only ever answers with the
 * selected version, so reading a candidate's content has to go through its own
 * resource.
 */
export function getScriptVersion(
  projectId: string,
  scriptId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ScriptProjection>> {
  return client.get<ScriptProjection>(
    `/api/v1/projects/${projectId}/scripts/${scriptId}`,
    signal ? { signal } : {},
  );
}
