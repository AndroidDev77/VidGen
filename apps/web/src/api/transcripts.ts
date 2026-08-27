import type {
  InvalidationSet,
  TranscriptProjection,
  TranscriptSegmentProjection,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export interface TranscriptSegmentUpdate {
  text?: string;
  speaker_label?: string;
  confirm_invalidation: boolean;
}

export interface TranscriptSegmentUpdateResult {
  segment: TranscriptSegmentProjection;
  transcript_row_version: number;
  invalidation: InvalidationSet;
}

export function getTranscript(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<TranscriptProjection>> {
  return client.get<TranscriptProjection>(
    `/api/v1/projects/${projectId}/transcript`,
    signal ? { signal } : {},
  );
}

export function updateTranscriptSegment(
  projectId: string,
  segmentId: string,
  update: TranscriptSegmentUpdate,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<TranscriptSegmentUpdateResult>> {
  return client.patch<TranscriptSegmentUpdateResult>(
    `/api/v1/projects/${projectId}/transcript/segments/${segmentId}`,
    { body: update, ifMatch: rowVersion, idempotencyKey },
  );
}
