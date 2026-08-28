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
  http.get(`${project}/visual-qa`, () => HttpResponse.json(fixtures.visualQaCollection(0))),
  http.get(`${project}/repairs`, () => HttpResponse.json(fixtures.repairCollection(0))),
  http.get(`${project}/shots/:shotId/repairs`, ({ params }) => {
    const index = fixtures.storyboard.shots.findIndex((shot) => shot.shot_id === params.shotId);
    if (index < 0) {
      return HttpResponse.json(apiError("not_found", "Shot not found."), { status: 404 });
    }
    return HttpResponse.json(fixtures.repairCollection(index));
  }),
  http.get(`${project}/shots/:shotId/repairs/:repairRunId`, () =>
    HttpResponse.json(fixtures.repairDetail(0)),
  ),
  // ``:act`` is an action suffix on the run path, so the handler matches the
  // raw URL rather than a path parameter.
  http.post(/\/repairs\/[^/]+:act$/, async ({ request }) => {
    const body = (await request.json()) as { action: string };
    return HttpResponse.json({
      repair_run_id: new URL(request.url).pathname.split("/").at(-1)!.split(":")[0],
      action: body.action,
      accepted: true,
      state: "REPAIR_PLANNING",
      code: "restarted_after_upstream_correction",
      row_version: 4,
    });
  }),
  http.get(`${project}/shots/:shotId/visual-qa`, ({ params }) => {
    const index = fixtures.storyboard.shots.findIndex((shot) => shot.shot_id === params.shotId);
    if (index < 0) {
      return HttpResponse.json(apiError("not_found", "Shot not found."), { status: 404 });
    }
    return HttpResponse.json(fixtures.visualQaCollection(index));
  }),
  http.get(`${project}/shots/:shotId/visual-qa/:qaRunId`, () =>
    HttpResponse.json(fixtures.visualQaDetail(0)),
  ),
  http.get(`${project}/shots/:shotId/visual-qa/:qaRunId/evidence`, () =>
    HttpResponse.json(fixtures.visualQaEvidence(0)),
  ),
  http.post(`${project}/shots/:shotId/visual-qa:run`, ({ params }) =>
    HttpResponse.json(
      {
        status: "queued",
        project_id: fixtures.PROJECT_ID,
        shot_id: params.shotId,
        targets: ["keyframe", "video"],
        resource_id: fixtures.uuid(1, 9),
        row_version: 3,
      },
      { status: 202 },
    ),
  ),
  // ``:approve`` and ``:reject`` are action suffixes on the run path, so the
  // handler matches the raw URL rather than a path parameter.
  http.post(/\/visual-qa\/[^/]+:(approve|reject)$/, ({ request }) =>
    HttpResponse.json({
      qa_run_id: new URL(request.url).pathname.split("/").at(-1)!.split(":")[0],
      review_id: fixtures.uuid(2, 9),
      decision: new URL(request.url).pathname.endsWith(":approve") ? "approved" : "rejected",
      resulting_gate: "visual_qa_human_approved",
      row_version: 4,
    }),
  ),
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
