import type {
  DisconnectResponse,
  OAuthStartRequest,
  OAuthStartResponse,
  PublicationCancelRequest,
  PublicationCollectionResponse,
  PublicationCreateRequest,
  PublicationDetailProjection,
  PublicationMetadataRequest,
  PublicationProjection,
  PublicationStartRequest,
  PublicationVisibilityRequest,
  YouTubeConnectionCollection,
} from "@vidgen/contracts";

import { apiClient, type ApiResponse, type VidGenClient } from "./client";

/**
 * The T25 publication control plane.
 *
 * Nothing here ever receives or sends a credential: an OAuth token, an
 * authorization code and a resumable session URI all stay on the backend. The
 * browser only ever holds identifiers, states, counters and the public watch
 * URL, which is why none of these functions has anywhere to put one.
 */

export function getYouTubeConnections(
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<YouTubeConnectionCollection>> {
  return client.get<YouTubeConnectionCollection>(
    "/api/v1/youtube/connections",
    signal ? { signal } : {},
  );
}

/**
 * Start an authorization. The returned URL is Google's own consent screen and
 * carries only the public client ID, the scopes, a one-time state and the PKCE
 * challenge - never a secret and never the verifier.
 */
export function startYouTubeOAuth(
  body: OAuthStartRequest,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<OAuthStartResponse>> {
  return client.post<OAuthStartResponse>("/api/v1/youtube/oauth:start", {
    body,
    idempotencyKey,
  });
}

export function disconnectYouTube(
  connectionId: string,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<DisconnectResponse>> {
  return client.delete<DisconnectResponse>(
    `/api/v1/youtube/connections/${connectionId}`,
    { idempotencyKey },
  );
}

export function getProjectPublications(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<PublicationCollectionResponse>> {
  return client.get<PublicationCollectionResponse>(
    `/api/v1/projects/${projectId}/publications`,
    signal ? { signal } : {},
  );
}

export function getPublication(
  projectId: string,
  publicationId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<PublicationDetailProjection>> {
  return client.get<PublicationDetailProjection>(
    `/api/v1/projects/${projectId}/publications/${publicationId}`,
    signal ? { signal } : {},
  );
}

export function createPublication(
  projectId: string,
  body: PublicationCreateRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<PublicationProjection>> {
  return client.post<PublicationProjection>(
    `/api/v1/projects/${projectId}/publications`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

/**
 * Save an edited draft on the existing publication.
 *
 * A PATCH, not another create: the create endpoint binds metadata into the
 * publication *identity*, so saving an edit that way would mint a new identity
 * and a second publication row for every save.
 */
export function updatePublicationDraft(
  projectId: string,
  publicationId: string,
  body: PublicationMetadataRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<PublicationProjection>> {
  return client.patch<PublicationProjection>(
    `/api/v1/projects/${projectId}/publications/${publicationId}`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

export function startPublication(
  projectId: string,
  publicationId: string,
  body: PublicationStartRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<PublicationProjection>> {
  return client.post<PublicationProjection>(
    `/api/v1/projects/${projectId}/publications/${publicationId}:start`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

/** Continue an interrupted upload from its server-confirmed byte offset. */
export function resumePublication(
  projectId: string,
  publicationId: string,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<PublicationProjection>> {
  return client.post<PublicationProjection>(
    `/api/v1/projects/${projectId}/publications/${publicationId}:resume`,
    { body: {}, ifMatch: rowVersion, idempotencyKey },
  );
}

export function cancelPublication(
  projectId: string,
  publicationId: string,
  body: PublicationCancelRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<PublicationProjection>> {
  return client.post<PublicationProjection>(
    `/api/v1/projects/${projectId}/publications/${publicationId}:cancel`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}

/**
 * Change the video's visibility. Always an explicit user action: nothing in the
 * pipeline makes a video unlisted or public on its own.
 */
export function changePublicationVisibility(
  projectId: string,
  publicationId: string,
  body: PublicationVisibilityRequest,
  rowVersion: number,
  idempotencyKey: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<PublicationProjection>> {
  return client.post<PublicationProjection>(
    `/api/v1/projects/${projectId}/publications/${publicationId}:visibility`,
    { body, ifMatch: rowVersion, idempotencyKey },
  );
}
