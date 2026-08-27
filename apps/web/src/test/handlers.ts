import { HttpResponse, http, type HttpHandler } from "msw";
import type { ApiError, ApiErrorCode } from "@vidgen/contracts";

import * as fixtures from "./fixtures";

const BASE = "http://localhost";

export function apiError(
  code: ApiErrorCode,
  summary: string,
  overrides: Partial<ApiError> = {},
): ApiError {
  return {
    schema_version: "1.0",
    code,
    summary,
    retryable: false,
    current_version: null,
    workflow_id: null,
    stage: null,
    fields: [],
    correlation_id: null,
    detail_code: null,
    ...overrides,
  };
}

const project = `${BASE}/api/v1/projects/:projectId`;

/** The default deterministic API surface every frontend test starts from. */
export const handlers: HttpHandler[] = [
  http.get(`${BASE}/api/v1/projects`, () => HttpResponse.json([fixtures.projectListItem])),
  http.post(`${BASE}/api/v1/projects`, () =>
    HttpResponse.json(fixtures.projectDetail, { status: 201 }),
  ),
  http.get(project, () => HttpResponse.json(fixtures.projectDetail)),
  http.get(`${project}/status`, () => HttpResponse.json(fixtures.projectStatus)),
  http.get(`${project}/workflow`, () => HttpResponse.json(fixtures.workflowStatus)),
  http.post(`${project}/workflow:start`, () =>
    HttpResponse.json({
      workflow_id: fixtures.workflowStatus.workflow_id,
      run_id: fixtures.workflowStatus.run_id,
      status: fixtures.workflowStatus,
    }),
  ),
  http.post(`${project}/workflow:cancel`, () => HttpResponse.json(fixtures.workflowStatus)),
  http.get(`${project}/events`, ({ request }) => {
    const url = new URL(request.url);
    if (url.searchParams.get("poll") === "true") {
      return HttpResponse.json({ items: [fixtures.projectEvent(1)], last_event_id: 1 });
    }
    return HttpResponse.text("", { headers: { "Content-Type": "text/event-stream" } });
  }),
  http.get(`${project}/transcript`, () => HttpResponse.json(fixtures.transcript)),
  http.get(`${project}/script`, () => HttpResponse.json(fixtures.script)),
  http.get(`${project}/scripts`, () => HttpResponse.json({ items: [fixtures.script.script] })),
  http.get(`${project}/storyboard`, () => HttpResponse.json(fixtures.storyboard)),
  http.get(`${project}/shots/:shotId`, ({ params }) => {
    const index = fixtures.storyboard.shots.findIndex((shot) => shot.shot_id === params.shotId);
    if (index < 0) {
      return HttpResponse.json(apiError("not_found", "Shot not found."), { status: 404 });
    }
    return HttpResponse.json(fixtures.shotDetail(index));
  }),
  http.get(`${project}/render`, () => HttpResponse.json(fixtures.render)),
  http.get(`${project}/costs`, () => HttpResponse.json(fixtures.costs)),
  http.get(`${project}/provider-attempts`, () => HttpResponse.json(fixtures.providerAttempts)),
  http.get(`${project}/failures`, () => HttpResponse.json(fixtures.failures)),
  http.get(`${BASE}/api/v1/assets/:assetId/download-url`, ({ params }) =>
    HttpResponse.json({
      asset_id: params.assetId,
      url: `${BASE}/blobs/${String(params.assetId)}?sig=short-lived`,
      expires_in_seconds: 900,
    }),
  ),
];
