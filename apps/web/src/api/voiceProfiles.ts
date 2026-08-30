import type {
  SelectVoiceProfileRequest,
  VoiceProfileListResponse,
  VoiceProfileResponse,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

/**
 * Narration voice selection.
 *
 * A project cannot start its workflow without one, so this is part of project
 * setup rather than an advanced setting. Nothing here sends or receives a
 * provider credential: the deployment resolves those in the worker.
 */
export function listVoiceProfiles(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<VoiceProfileListResponse>> {
  return client.get<VoiceProfileListResponse>(
    `/api/v1/projects/${projectId}/voice-profiles`,
    signal ? { signal } : {},
  );
}

export function getVoiceProfile(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<VoiceProfileResponse>> {
  return client.get<VoiceProfileResponse>(
    `/api/v1/projects/${projectId}/voice-profile`,
    signal ? { signal } : {},
  );
}

export function selectVoiceProfile(
  projectId: string,
  body: SelectVoiceProfileRequest,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<VoiceProfileResponse>> {
  return client.put<VoiceProfileResponse>(`/api/v1/projects/${projectId}/voice-profile`, {
    body,
  });
}
