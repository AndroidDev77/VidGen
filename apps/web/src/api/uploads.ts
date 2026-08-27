import { apiClient, type ApiResponse, type VidGenClient } from "./client";

export interface UploadSession {
  id: string;
  project_id: string;
  filename: string;
  media_type: string;
  expected_size: number;
  expected_sha256: string;
  part_size: number;
  status: string;
  completed_asset_id: string | null;
  error_code: string | null;
}

export interface UploadPartResult {
  upload_id: string;
  part_number: number;
  byte_size: number;
  sha256: string;
  duplicate: boolean;
}

export interface CompletedUpload {
  upload_id: string;
  source_video_id: string;
  asset_id: string;
  sha256: string;
  byte_size: number;
  status: string;
}

export interface InitializeUploadInput {
  filename: string;
  media_type: string;
  expected_size: number;
  expected_sha256: string;
  part_size: number;
}

export function initializeUpload(
  projectId: string,
  input: InitializeUploadInput,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<UploadSession>> {
  return client.post<UploadSession>(`/api/v1/projects/${projectId}/uploads`, { body: input });
}

export function uploadPart(
  uploadId: string,
  partNumber: number,
  bytes: ArrayBuffer | Uint8Array | Blob,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<UploadPartResult>> {
  return client.put<UploadPartResult>(`/api/v1/uploads/${uploadId}/parts/${partNumber}`, {
    rawBody: bytes as BodyInit,
    contentType: "application/octet-stream",
    ...(signal ? { signal } : {}),
  });
}

export function completeUpload(
  uploadId: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<CompletedUpload>> {
  return client.post<CompletedUpload>(`/api/v1/uploads/${uploadId}/complete`);
}

export interface DownloadUrl {
  asset_id: string;
  url: string;
  expires_in_seconds: number;
}

/**
 * Signed download URLs are requested shortly before use and never persisted:
 * they are ephemeral capabilities, not application state.
 */
export function getDownloadUrl(
  assetId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<DownloadUrl>> {
  return client.get<DownloadUrl>(
    `/api/v1/assets/${assetId}/download-url`,
    signal ? { signal } : {},
  );
}
