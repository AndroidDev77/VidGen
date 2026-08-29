import type { Page, Route } from "@playwright/test";

/**
 * A deterministic in-browser VidGen API.
 *
 * Every response is generated here with synthetic media and fake providers, so
 * the browser acceptance run never contacts OpenAI, Runway, ElevenLabs, or any
 * other paid service, and needs no Temporal cluster.
 */
export const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
export const UPLOAD_ID = "22222222-2222-4222-8222-222222222222";
export const SHOT_COUNT = 10;
const HASH = "a".repeat(64);

export interface FakeApiState {
  workflowStarts: string[];
  uploadedParts: number[];
  uploadComplete: boolean;
  scriptVersion: number;
  scriptText: Record<number, string>;
  transcriptText: Record<number, string>;
  regeneratedShots: string[];
  shotIdentities: Record<string, string>;
  renderStale: boolean;
  approvals: number;
  downloads: string[];
  providerAttempts: number;
  /** Recorded T20 human-review decisions, newest last. */
  visualQaDecisions: string[];
  /** Recorded T22 final-QA runs requested by the owner, newest last. */
  finalQaRuns: string[];
  /** Recorded T22 semantic review resolutions, newest last. */
  finalQaReviews: string[];
  /** Whether the T22 review finding has been resolved, which clears the gate. */
  finalQaResolved: boolean;
}

export function shotId(index: number): string {
  return `66666666-2222-4222-8222-${String(index).padStart(12, "0")}`;
}

function scriptSegmentId(index: number): string {
  return `55555555-2222-4222-8222-${String(20 + index).padStart(12, "0")}`;
}

function transcriptSegmentId(index: number): string {
  return `44444444-2222-4222-8222-${String(10 + index).padStart(12, "0")}`;
}

export function createFakeState(): FakeApiState {
  const shotIdentities: Record<string, string> = {};
  for (let index = 0; index < SHOT_COUNT; index += 1) {
    shotIdentities[shotId(index)] = `identity-${index}`;
  }
  return {
    workflowStarts: [],
    uploadedParts: [],
    uploadComplete: false,
    scriptVersion: 1,
    scriptText: {},
    transcriptText: {},
    regeneratedShots: [],
    shotIdentities,
    renderStale: false,
    approvals: 0,
    downloads: [],
    providerAttempts: SHOT_COUNT,
    visualQaDecisions: [],
    finalQaRuns: [],
    finalQaReviews: [],
    finalQaResolved: false,
  };
}

export const FINAL_QA_RUN_ID = "aaaaaaaa-2222-4222-8222-000000000001";
export const FINAL_QA_FINDING_ID = "aaaaaaaa-2222-4222-8222-000000000002";
const FINAL_RENDER_ASSET_ID = "77777777-2222-4222-8222-000000000001";
const FINAL_MANIFEST_ASSET_ID = "77777777-2222-4222-8222-000000000002";
const TIMELINE_US = 30_000_000;

/** The T22 run projection: a delivery that measures clean but reads uncertain. */
function finalQaRun(state: FakeApiState) {
  const resolved = state.finalQaResolved;
  return {
    final_editorial_run_id: FINAL_QA_RUN_ID,
    project_id: PROJECT_ID,
    final_render_asset_id: FINAL_RENDER_ASSET_ID,
    render_manifest_asset_id: FINAL_MANIFEST_ASSET_ID,
    render_identity: HASH,
    final_qa_identity: "b".repeat(64),
    input_hash: "c".repeat(64),
    configuration_hash: "d".repeat(64),
    report_version: "final-editorial/1.0",
    status: resolved ? "FINAL_QA_PASSED" : "FINAL_QA_REVIEW_REQUIRED",
    phase: "COMPLETION_GATE",
    decision: resolved ? "PASS" : "REVIEW",
    selected: true,
    blocking_finding_count: 0,
    review_finding_count: resolved ? 0 : 1,
    warning_finding_count: 0,
    deterministic_failure_count: 0,
    remediation_targets: resolved ? [] : ["HUMAN_EDITORIAL_REVIEW"],
    provider: "fake",
    model: "fake-final-editorial-1",
    adjudicated: true,
    cost_microusd: 54_000,
    report_asset_id: "aaaaaaaa-2222-4222-8222-000000000003",
    contact_sheet_asset_id: null,
    error_code: null,
    row_version: 1,
    created_at: "2026-08-02T12:00:00Z",
    completed_at: "2026-08-02T12:02:00Z",
  };
}

function finalQaDetail(state: FakeApiState) {
  return {
    ...finalQaRun(state),
    measurements: {
      container_format: "mov,mp4,m4a",
      byte_size: 12_345_678,
      video_codec: "h264",
      audio_codec: "aac",
      width: 1920,
      height: 1080,
      pixel_format: "yuv420p",
      frame_rate: "24/1",
      container_duration_us: TIMELINE_US,
      video_duration_us: TIMELINE_US,
      audio_duration_us: TIMELINE_US,
      sample_rate_hz: 48_000,
      channels: 2,
      integrated_lufs: -14.2,
      true_peak_dbtp: -1.4,
      clipping_ratio: 0,
      video_decoded: true,
      audio_decoded: true,
      black_interval_count: 0,
      freeze_interval_count: 0,
      silence_interval_count: 1,
      ffmpeg_version: "ffmpeg 7",
      ffprobe_version: "ffprobe 7",
    },
    media_checks: [
      {
        check_id: "aaaaaaaa-2222-4222-8222-000000000010",
        check_type: "media",
        code: "RESOLUTION_MISMATCH",
        status: "pass",
        blocking: false,
        measurement: null,
        threshold: null,
        unit: "",
        start_us: null,
        end_us: null,
        cue_sequence: null,
        tool: "ffprobe",
        tool_version: "ffprobe 7",
        message: "resolution matches the delivery profile",
      },
    ],
    audio_checks: [],
    caption_checks: [],
    dimensions: [
      {
        category: "comprehensibility",
        applicable: true,
        score: 92,
        confidence: 0.71,
        blocking_finding_count: 0,
        review_finding_count: 1,
        warning_finding_count: 0,
        summary: "",
      },
    ],
    findings: state.finalQaResolved
      ? []
      : [
          {
            finding_id: FINAL_QA_FINDING_ID,
            category: "comprehensibility",
            severity: "review_required",
            blocking: false,
            confidence: 0.62,
            issue_code: "INCOMPREHENSIBLE_SEQUENCE",
            summary: "The jump between shots four and five may confuse a viewer.",
            start_us: 9_000_000,
            end_us: 12_000_000,
            shot_ids: [shotId(4)],
            caption_cue_sequences: [],
            narration_segment_ids: [],
            evidence: [
              {
                evidence_id: "aaaaaaaa-2222-4222-8222-000000000020",
                evidence_type: "contact_sheet_tile",
                start_us: 9_000_000,
                end_us: 9_000_000,
                frame_asset_id: null,
                sample_id: null,
                contact_sheet_asset_id: null,
                contact_sheet_position: 4,
                caption_cue_sequence: null,
                shot_id: shotId(4),
                measurement: null,
                threshold: null,
                explanation: "sampled frame",
              },
            ],
            expected_behavior: "the cut reads clearly",
            observed_behavior: "the cut may not read",
            remediation_target: "HUMAN_EDITORIAL_REVIEW",
            provenance: "provider",
            resolved_by_review: false,
          },
        ],
    remediation_routes: [],
    adjudication_confidence: 0.62,
    adjudication_decided: false,
    gate_reasons: state.finalQaResolved ? [] : ["1 unresolved review finding(s)"],
    timeline_duration_us: TIMELINE_US,
  };
}

function finalQaGate(state: FakeApiState) {
  const resolved = state.finalQaResolved;
  return {
    project_id: PROJECT_ID,
    final_editorial_run_id: FINAL_QA_RUN_ID,
    final_render_asset_id: FINAL_RENDER_ASSET_ID,
    decision: resolved ? "PASS" : "REVIEW",
    allowed: resolved,
    reason: resolved ? "final_qa_pass" : "final_qa_review_required",
    blocking_finding_count: 0,
    review_finding_count: resolved ? 0 : 1,
    deterministic_failure_count: 0,
    gate_version: "final-gate/1.0",
    row_version: 1,
  };
}

const QA_DIMENSIONS: readonly (readonly [string, number])[] = [
  ["character_identity", 25],
  ["character_count", 10],
  ["location", 10],
  ["wardrobe_and_state", 10],
  ["action_and_motion", 15],
  ["composition", 10],
  ["anatomy_and_artifacts", 10],
  ["continuity_and_style", 10],
];

export function visualQaRunId(index: number, target: "keyframe" | "video"): string {
  const offset = target === "keyframe" ? 600 : 700;
  return `99999999-2222-4222-8222-${String(offset + index).padStart(12, "0")}`;
}

function visualQaSampleId(index: number): string {
  return `99999999-2222-4222-8222-${String(800 + index).padStart(12, "0")}`;
}

function visualQaFrameAssetId(index: number): string {
  return `99999999-2222-4222-8222-${String(900 + index).padStart(12, "0")}`;
}

function visualQaReferenceAssetId(index: number): string {
  return `99999999-2222-4222-8222-${String(950 + index).padStart(12, "0")}`;
}

/** One compact QA run. Shot 5 carries the blocking identity failure. */
function visualQaRun(index: number, target: "keyframe" | "video") {
  const blocking = target === "video" && index === 5;
  return {
    qa_run_id: visualQaRunId(index, target),
    project_id: PROJECT_ID,
    shot_id: shotId(index),
    target_type: target,
    status: "visual_qa_complete",
    outcome: blocking ? "FAIL" : "PASS",
    score: blocking ? 82.5 : 96,
    pass_threshold: 85,
    importance: "normal",
    hard_failure: blocking,
    repair_recommendation: blocking ? "NEW_SEED" : "NONE",
    repair_codes: blocking ? ["WRONG_CHARACTER_IDENTITY"] : [],
    warning_codes: blocking ? ["excessive_freeze"] : [],
    confidence: 0.91,
    adjudicated: blocking,
    human_review_decision: null,
    provider: "fake",
    model: "fake-visual-qa/1",
    cost_microusd: 24_000,
    rubric_version: "visual-qa-rubric/1.0",
    threshold_version: "visual-qa-thresholds/1.0",
    sampling_version: "visual-qa-sampler/1.0",
    sample_count: 12,
    deterministic_warning_count: blocking ? 2 : 0,
    row_version: 1,
    created_at: "2026-08-02T11:00:00Z",
    completed_at: "2026-08-02T11:01:00Z",
  };
}

function visualQaSample(index: number) {
  return {
    sample_id: visualQaSampleId(index),
    sequence: 0,
    sample_type: "action_window",
    requested_timestamp_us: 1_500_000,
    actual_timestamp_us: 1_500_000,
    shot_relative_timestamp_us: 1_500_000,
    frame_asset_id: visualQaFrameAssetId(index),
    frame_sha256: HASH,
    selection_reason: "inside the required action window (1/3)",
    contact_sheet_position: 0,
  };
}

function visualQaDetail(index: number, target: "keyframe" | "video") {
  const blocking = target === "video" && index === 5;
  return {
    ...visualQaRun(index, target),
    dimensions: QA_DIMENSIONS.map(([dimension, weight]) => {
      const raw = blocking && dimension === "character_identity" ? 40 : 95;
      return {
        dimension,
        applicable: true,
        raw_score: raw,
        weight,
        effective_weight: weight,
        weighted_contribution: (raw * weight) / 100,
        confidence: 0.9,
        warning_codes: [],
        hard_failure_codes:
          blocking && dimension === "character_identity" ? ["WRONG_CHARACTER_IDENTITY"] : [],
        repair_codes:
          blocking && dimension === "character_identity" ? ["WRONG_CHARACTER_IDENTITY"] : [],
        finding_summaries:
          blocking && dimension === "character_identity"
            ? ["The subject does not match the approved identity reference."]
            : [],
      };
    }),
    diagnostics: [
      {
        code: "freeze_ratio",
        outcome: blocking ? "warning" : "pass",
        diagnostic_code: blocking ? "excessive_freeze" : "freeze_ratio_ok",
        measurement: blocking ? 0.51 : 0.02,
        threshold: 0.35,
        evidence_timestamp_us: 1_500_000,
        repair_code: blocking ? "EXCESSIVE_FREEZE" : null,
        message: "",
      },
    ],
    samples: [visualQaSample(index)],
    compared_reference_asset_ids: [visualQaReferenceAssetId(index)],
    contact_sheet_asset_id: null,
    report_asset_id: null,
    adjudication: blocking
      ? {
          policy_version: "visual-qa-adjudication/1.0",
          triggered_by: ["first-pass character_identity confidence 0.62 is below 0.70"],
          first_pass_provider: "fake",
          first_pass_model: "fake-visual-qa/1",
          adjudicator_provider: "fake",
          adjudicator_model: "fake-visual-qa-adjudicator/1",
          adjudicator_confidence: 0.86,
          decided: true,
          disagreement_summary: [],
          resulting_outcome_hint: "FAIL",
          attempts_used: 1,
        }
      : null,
  };
}

function visualQaEvidence(index: number) {
  return {
    qa_run_id: visualQaRunId(index, "video"),
    items: [
      {
        evidence_id: `99999999-2222-4222-8222-${String(1000 + index).padStart(12, "0")}`,
        finding_id: `99999999-2222-4222-8222-${String(1100 + index).padStart(12, "0")}`,
        evidence_type: "reference_comparison",
        sample_id: visualQaSampleId(index),
        frame_asset_id: visualQaFrameAssetId(index),
        shot_relative_timestamp_us: 1_500_000,
        source_relative_timestamp_us: 1_500_000,
        contact_sheet_position: 0,
        bounding_box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
        compared_reference_asset_id: visualQaReferenceAssetId(index),
        confidence: 0.93,
        explanation: "Face geometry does not match the approved identity version.",
      },
    ],
    samples: [visualQaSample(index)],
  };
}

function shotIndexFor(path: string): number {
  const id = path.split("/shots/")[1]?.split("/")[0]?.split(":")[0] ?? "";
  return (
    Array.from({ length: SHOT_COUNT }, (_, value) => value).find(
      (value) => shotId(value) === id,
    ) ?? 0
  );
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: { ETag: '"1"' },
    body: JSON.stringify(body),
  });
}

/** Install the fake API and its synthetic assets on one page. */
export async function installFakeApi(page: Page, state: FakeApiState): Promise<void> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/projects" && method === "GET") {
      return json(route, [projectListItem()]);
    }
    if (path === "/api/v1/projects" && method === "POST") {
      return json(route, projectDetail(), 201);
    }
    if (path === `/api/v1/projects/${PROJECT_ID}/uploads`) {
      return json(route, uploadSession(), 201);
    }
    if (path.startsWith(`/api/v1/uploads/${UPLOAD_ID}/parts/`)) {
      const part = Number(path.split("/").pop());
      state.uploadedParts.push(part);
      return json(route, {
        upload_id: UPLOAD_ID,
        part_number: part,
        byte_size: 1,
        sha256: "b".repeat(64),
        duplicate: false,
      });
    }
    if (path === `/api/v1/uploads/${UPLOAD_ID}/complete`) {
      state.uploadComplete = true;
      return json(route, {
        upload_id: UPLOAD_ID,
        source_video_id: "33333333-3333-4333-8333-333333333333",
        asset_id: "44444444-4444-4444-8444-444444444444",
        sha256: HASH,
        byte_size: 1,
        status: "completed",
      });
    }
    if (path.endsWith("/workflow:start")) {
      state.workflowStarts.push(request.headers()["idempotency-key"] ?? "");
      return json(route, {
        workflow_id: `vidgen-project-${PROJECT_ID}`,
        run_id: `vidgen-project-${PROJECT_ID}-run`,
        status: workflowStatus(),
      });
    }
    if (path.endsWith("/workflow")) {
      return json(route, workflowStatus());
    }
    if (path.endsWith("/events")) {
      if (url.searchParams.get("poll") === "true") {
        return json(route, { items: [progressEvent()], last_event_id: 1 });
      }
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `id: 1\nevent: workflow_started\ndata: ${JSON.stringify(progressEvent())}\n\n`,
      });
    }
    if (path.endsWith("/status")) {
      return json(route, {
        project_id: PROJECT_ID,
        status: "review",
        source_video_id: "33333333-3333-4333-8333-333333333333",
        source_asset_id: "44444444-4444-4444-8444-444444444444",
        upload_status: "completed",
        error_code: null,
      });
    }
    if (path.endsWith("/transcript")) {
      return json(route, transcript(state));
    }
    if (path.includes("/transcript/segments/")) {
      const id = path.split("/").pop() ?? "";
      const index = [0, 1].find((value) => transcriptSegmentId(value) === id) ?? 0;
      const body = request.postDataJSON() as { text?: string };
      state.transcriptText[index] = body.text ?? "";
      return json(route, {
        segment: transcriptSegment(state, index),
        transcript_row_version: 2,
        invalidation: { schema_version: "1.0", entries: [], requires_confirmation: false },
      });
    }
    if (path.endsWith("/scripts")) {
      return json(route, { items: [scriptSummary(state)] });
    }
    if (path.endsWith("/script")) {
      return json(route, script(state));
    }
    if (path.includes("/script-segments/")) {
      const id = path.split("/").pop() ?? "";
      const index =
        Array.from({ length: SHOT_COUNT }, (_, value) => value).find(
          (value) => scriptSegmentId(value) === id,
        ) ?? 0;
      const body = request.postDataJSON() as { text?: string };
      state.scriptText[index] = body.text ?? "";
      state.scriptVersion += 1;
      state.renderStale = true;
      return json(route, {
        segment: scriptSegment(state, index),
        script: scriptSummary(state),
        created_version: true,
        invalidation: {
          schema_version: "1.0",
          requires_confirmation: true,
          entries: [
            {
              schema_version: "1.0",
              resource_type: "render",
              resource_id: "88888888-2222-4222-8222-000000000001",
              label: "Verified render attempt 1",
              reason: "script_edited",
            },
          ],
        },
      });
    }
    if (path.endsWith("/storyboard")) {
      return json(route, storyboard(state));
    }
    if (path.includes(":regenerate")) {
      const id = path.split("/").pop()?.replace(":regenerate", "") ?? "";
      state.regeneratedShots.push(id);
      state.shotIdentities[id] = `identity-regenerated-${state.regeneratedShots.length}`;
      state.renderStale = true;
      return json(route, {
        schema_version: "1.0",
        shot_id: id,
        child_workflow_id: `vidgen-shot-${id}-regenerated`,
        new_identity_hash: "b".repeat(64),
        previous_identity_hash: HASH,
        preserved_attempt_ids: [],
        invalidation: {
          schema_version: "1.0",
          requires_confirmation: true,
          entries: [
            {
              schema_version: "1.0",
              resource_type: "shot",
              resource_id: id,
              label: "Shot 6",
              reason: "shot_regenerated",
            },
          ],
        },
        row_version: 2,
      });
    }
    if (path.includes("/visual-qa") && method === "POST") {
      const index = shotIndexFor(path);
      if (path.endsWith(":approve") || path.endsWith(":reject")) {
        const decision = path.endsWith(":approve") ? "approved" : "rejected";
        state.visualQaDecisions.push(decision);
        return json(route, {
          qa_run_id: visualQaRunId(index, "video"),
          review_id: "99999999-2222-4222-8222-000000009999",
          decision,
          resulting_gate:
            decision === "approved" ? "visual_qa_human_approved" : "visual_qa_failed",
          row_version: 2,
        });
      }
      return json(
        route,
        {
          status: "queued",
          project_id: PROJECT_ID,
          shot_id: shotId(index),
          targets: ["keyframe", "video"],
          resource_id: "99999999-2222-4222-8222-000000008888",
          row_version: 1,
        },
        202,
      );
    }
    if (path.endsWith("/evidence") && method === "GET") {
      return json(route, visualQaEvidence(shotIndexFor(path)));
    }
    if (path.includes("/visual-qa/") && method === "GET") {
      const index = shotIndexFor(path);
      const runId = path.split("/").pop() ?? "";
      const target = runId === visualQaRunId(index, "keyframe") ? "keyframe" : "video";
      return json(route, visualQaDetail(index, target));
    }
    if (path.endsWith("/visual-qa") && method === "GET") {
      if (path.includes("/shots/")) {
        const index = shotIndexFor(path);
        return json(route, {
          project_id: PROJECT_ID,
          items: [visualQaRun(index, "keyframe"), visualQaRun(index, "video")],
        });
      }
      return json(route, {
        project_id: PROJECT_ID,
        items: Array.from({ length: SHOT_COUNT }, (_, index) => [
          visualQaRun(index, "keyframe"),
          visualQaRun(index, "video"),
        ]).flat(),
      });
    }
    if (path.includes("/shots/") && method === "GET") {
      const id = path.split("/").pop() ?? "";
      const index =
        Array.from({ length: SHOT_COUNT }, (_, value) => value).find(
          (value) => shotId(value) === id,
        ) ?? 0;
      return json(route, shotDetail(state, index));
    }
    if (path.endsWith("/final-qa:run") && method === "POST") {
      state.finalQaRuns.push(request.headers()["idempotency-key"] ?? "");
      return json(
        route,
        {
          status: "queued",
          project_id: PROJECT_ID,
          final_render_asset_id: FINAL_RENDER_ASSET_ID,
          provider: "fake",
          resource_id: "aaaaaaaa-2222-4222-8222-000000000030",
          row_version: 1,
        },
        202,
      );
    }
    if (path.endsWith(":review") && path.includes("/final-qa/") && method === "POST") {
      state.finalQaReviews.push(request.headers()["idempotency-key"] ?? "");
      state.finalQaResolved = true;
      return json(route, {
        final_editorial_run_id: FINAL_QA_RUN_ID,
        review_id: "aaaaaaaa-2222-4222-8222-000000000031",
        finding_id: FINAL_QA_FINDING_ID,
        decision: "accept",
        resulting_gate: "PASS",
        row_version: 2,
      });
    }
    if (path.endsWith("/final-qa/gate") && method === "GET") {
      return json(route, finalQaGate(state));
    }
    if (path.includes("/final-qa/") && method === "GET") {
      return json(route, finalQaDetail(state));
    }
    if (path.endsWith("/final-qa") && method === "GET") {
      return json(route, { project_id: PROJECT_ID, items: [finalQaRun(state)] });
    }
    if (path.endsWith("/render:start")) {
      state.renderStale = false;
      return json(route, { render: render(state) });
    }
    if (path.endsWith("/render")) {
      return json(route, render(state));
    }
    if (path.endsWith("/review:approve")) {
      state.approvals += 1;
      const approval = {
        schema_version: "1.0",
        approval_id: "99999999-2222-4222-8222-000000000001",
        render_job_id: "88888888-2222-4222-8222-000000000001",
        approved_by: "local-user",
        approved_at: "2026-08-02T09:00:00Z",
        lineage_hash: HASH,
        applies_to_current_lineage: true,
      };
      return json(route, { approval, render: { ...render(state), approval } });
    }
    if (path.endsWith("/costs")) {
      return json(route, costs());
    }
    if (path.endsWith("/provider-attempts")) {
      return json(route, {
        total: state.providerAttempts,
        offset: 0,
        limit: 10,
        items: [],
      });
    }
    if (path.endsWith("/failures")) {
      return json(route, { items: [] });
    }
    if (path.includes("/download-url")) {
      const assetId = path.split("/")[4] ?? "";
      state.downloads.push(assetId);
      return json(route, {
        asset_id: assetId,
        url: `/fake-assets/${assetId}`,
        expires_in_seconds: 60,
      });
    }
    // T25 publication. Declared before the project catch-all below, which would
    // otherwise answer these paths with a project detail body.
    if (path === "/api/v1/youtube/connections" && method === "GET") {
      return json(route, youtubeConnections());
    }
    if (path.endsWith("/publications") && method === "GET") {
      return json(route, publicationCollection(state));
    }
    if (path.includes("/publications/") && method === "GET") {
      return json(route, publicationDetail());
    }
    if (path.startsWith(`/api/v1/projects/${PROJECT_ID}`) && method === "GET") {
      return json(route, projectDetail());
    }
    return json(route, { code: "not_found", summary: "Not found." }, 404);
  });

  // Synthetic assets: a tiny WebVTT track and stub binaries, never real media.
  await page.route("**/fake-assets/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/vtt",
      body: "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nSynthetic caption cue.\n",
    }),
  );
}

function projectListItem() {
  return {
    id: PROJECT_ID,
    name: "Season 3 Episode 4",
    status: "review",
    target_duration_seconds: 300,
    visual_style: "flat editorial cartoon",
    humor_intensity: 6,
    created_at: "2026-08-01T09:00:00Z",
    updated_at: "2026-08-02T10:30:00Z",
    current_stage: "review",
    progress_percentage: 100,
    committed_cost_amount: "1.000000",
    hard_cap_amount: "20.000000",
    has_failures: false,
    row_version: 1,
  };
}

function projectDetail() {
  return {
    id: PROJECT_ID,
    name: "Season 3 Episode 4",
    status: "review",
    target_duration_seconds: 300,
    visual_style: "flat editorial cartoon",
    humor_intensity: 6,
    created_at: "2026-08-01T09:00:00Z",
    updated_at: "2026-08-02T10:30:00Z",
  };
}

function uploadSession() {
  return {
    id: UPLOAD_ID,
    project_id: PROJECT_ID,
    filename: "episode.mp4",
    media_type: "video/mp4",
    expected_size: 12,
    expected_sha256: HASH,
    part_size: 4,
    status: "pending",
    completed_asset_id: null,
    error_code: null,
  };
}

const STAGES = [
  "upload",
  "media_processing",
  "transcript_acquisition",
  "evidence",
  "episode_analysis",
  "script_generation",
  "narration",
  "storyboard",
  "keyframes",
  "animation",
  "shot_orchestration",
  "captions",
  "rendering",
  "review",
] as const;

function workflowStatus() {
  return {
    schema_version: "1.0",
    project_id: PROJECT_ID,
    workflow_id: `vidgen-project-${PROJECT_ID}`,
    run_id: `vidgen-project-${PROJECT_ID}-run`,
    status: "review",
    current_stage: "review",
    completed_stages: [...STAGES],
    cancelled: false,
    started_at: "2026-08-01T09:05:00Z",
    updated_at: "2026-08-01T10:05:00Z",
    elapsed_seconds: 3600,
    total_shot_count: SHOT_COUNT,
    completed_shot_count: SHOT_COUNT,
    failed_shot_count: 0,
    retryable_failure_count: 0,
    render_status: "render_complete",
    stages: STAGES.map((stage) => ({
      schema_version: "1.0",
      stage,
      state: "complete",
      started_at: null,
      completed_at: null,
      detail_code: null,
    })),
    progress_percentage: 100,
  };
}

function progressEvent() {
  return {
    schema_version: "1.0",
    event_id: 1,
    project_id: PROJECT_ID,
    workflow_id: `vidgen-project-${PROJECT_ID}`,
    event_type: "workflow_started",
    stage: "upload",
    status: "running",
    progress_percentage: null,
    completed_shot_count: null,
    total_shot_count: SHOT_COUNT,
    retryable_failure_count: null,
    render_status: null,
    cost_summary_version: null,
    warning_code: null,
    failure_code: null,
    created_at: "2026-08-01T09:05:00Z",
  };
}

function transcriptSegment(state: FakeApiState, index: number) {
  return {
    schema_version: "1.0",
    segment_id: transcriptSegmentId(index),
    sequence: index,
    start_seconds: index * 10,
    end_seconds: index * 10 + 9,
    text: state.transcriptText[index] ?? `Transcript line ${index + 1}.`,
    speaker_label: `SPEAKER_0${index % 2}`,
    confidence: 0.9,
    edited: state.transcriptText[index] !== undefined,
    row_version: state.transcriptText[index] === undefined ? 1 : 2,
  };
}

function transcript(state: FakeApiState) {
  return {
    schema_version: "1.0",
    transcript_id: "44444444-2222-4222-8222-000000000001",
    project_id: PROJECT_ID,
    version: 1,
    language: "en",
    origin: "transcription",
    duration_seconds: 1800,
    coverage_score: 0.98,
    selected: true,
    row_version: 1,
    source_asset_id: "44444444-2222-4222-8222-000000000002",
    segments: [transcriptSegment(state, 0), transcriptSegment(state, 1)],
  };
}

function scriptSegment(state: FakeApiState, index: number) {
  return {
    schema_version: "1.0",
    segment_id: scriptSegmentId(index),
    stable_segment_id: `55555555-2222-4222-8222-${String(100 + index).padStart(12, "0")}`,
    sequence: index,
    segment_type: "narration",
    speaker_kind: "narrator",
    speaker_label: null,
    text: state.scriptText[index] ?? `Recap beat number ${index + 1} lands with a joke.`,
    visual_gag: null,
    joke_annotation_count: 1,
    plot_beat_ids: [],
    word_count: 8,
    estimated_duration_ms: 3000,
    measured_narration_duration_ms: 3000,
    locked: false,
    content_hash: HASH,
    row_version: state.scriptText[index] === undefined ? 1 : 2,
  };
}

function scriptSummary(state: FakeApiState) {
  return {
    schema_version: "1.0",
    script_id: "55555555-2222-4222-8222-000000000001",
    version: state.scriptVersion,
    status: "approved",
    selected: true,
    actual_word_count: 80,
    target_word_count: 700,
    target_duration_ms: 300_000,
    parent_script_id: null,
    created_at: "2026-08-01T09:30:00Z",
    row_version: state.scriptVersion,
  };
}

function script(state: FakeApiState) {
  return {
    schema_version: "1.0",
    project_id: PROJECT_ID,
    script: scriptSummary(state),
    approved: true,
    segments: Array.from({ length: SHOT_COUNT }, (_, index) => scriptSegment(state, index)),
  };
}

function storyboardShot(state: FakeApiState, index: number) {
  const id = shotId(index);
  const regenerated = state.regeneratedShots.includes(id);
  return {
    schema_version: "1.0",
    shot_id: id,
    stable_shot_id: `66666666-2222-4222-8222-${String(100 + index).padStart(12, "0")}`,
    global_sequence: index,
    segment_sequence: 0,
    script_segment_id: scriptSegmentId(index),
    global_start_us: index * 3_000_000,
    global_end_us: (index + 1) * 3_000_000,
    usable_duration_us: 3_000_000,
    requested_generation_duration_us: 4_000_000,
    trim_start_us: 0,
    trim_end_us: 1_000_000,
    visual_objective: `Show beat ${index + 1} in a wide comic frame.`,
    camera_framing: "medium",
    camera_movement: "static",
    character_references: ["protagonist"],
    location_reference: "set",
    transition_in: "cut",
    transition_out: "cut",
    workflow_status: "locked",
    selected_keyframe_asset_id: `77777777-2222-4222-8222-${String(200 + index).padStart(12, "0")}`,
    selected_video_asset_id: `77777777-2222-4222-8222-${String(300 + index).padStart(12, "0")}`,
    provider: "fake",
    model: "fake-video",
    attempt_count: regenerated ? 2 : 1,
    cost_amount: "0.100000",
    warning_code: null,
    failure_code: null,
    row_version: regenerated ? 2 : 1,
  };
}

function storyboard(state: FakeApiState) {
  return {
    schema_version: "1.0",
    project_id: PROJECT_ID,
    storyboard_run_id: "66666666-2222-4222-8222-000000000001",
    version: 1,
    selected: true,
    shot_count: SHOT_COUNT,
    segment_count: SHOT_COUNT,
    total_duration_us: SHOT_COUNT * 3_000_000,
    timing_manifest_asset_id: "66666666-2222-4222-8222-000000000002",
    row_version: 1,
    shots: Array.from({ length: SHOT_COUNT }, (_, index) => storyboardShot(state, index)),
  };
}

function shotDetail(state: FakeApiState, index: number) {
  const id = shotId(index);
  return {
    schema_version: "1.0",
    shot: storyboardShot(state, index),
    child_workflow_id: `vidgen-shot-${id}`,
    child_workflow_status: "locked",
    child_workflow_retryable: false,
    identity_hash: HASH,
    trim_instructions_asset_id: null,
    source_evidence_ids: [],
    keyframe_attempts: [
      {
        schema_version: "1.0",
        attempt_id: `77777777-2222-4222-8222-${String(500 + index).padStart(12, "0")}`,
        kind: "keyframe",
        attempt_number: 1,
        status: "succeeded",
        asset_id: `77777777-2222-4222-8222-${String(200 + index).padStart(12, "0")}`,
        provider: "fake",
        model: "fake-image",
        provider_task_id: null,
        generation_identity: state.shotIdentities[id] ?? HASH,
        prompt_version: "v1",
        generated_duration_us: null,
        usable_duration_us: null,
        cost_amount: null,
        failure_class: null,
        selected: true,
        created_at: "2026-08-01T09:59:00Z",
      },
    ],
    video_attempts: [
      {
        schema_version: "1.0",
        attempt_id: `77777777-2222-4222-8222-${String(400 + index).padStart(12, "0")}`,
        kind: "video",
        attempt_number: 1,
        status: "succeeded",
        asset_id: `77777777-2222-4222-8222-${String(300 + index).padStart(12, "0")}`,
        provider: "fake",
        model: "fake-video",
        provider_task_id: `fake-task-${index}`,
        generation_identity: state.shotIdentities[id] ?? HASH,
        prompt_version: "v1",
        generated_duration_us: 4_000_000,
        usable_duration_us: 3_000_000,
        cost_amount: "0.100000",
        failure_class: null,
        selected: true,
        created_at: "2026-08-01T10:00:00Z",
      },
    ],
    regeneration_history: state.regeneratedShots.filter((entry) => entry === id),
  };
}

function render(state: FakeApiState) {
  return {
    schema_version: "1.0",
    render_job_id: "88888888-2222-4222-8222-000000000001",
    project_id: PROJECT_ID,
    status: "render_complete",
    attempt: 1,
    render_version: "t17/1",
    render_identity: HASH,
    selected: true,
    stale: state.renderStale,
    verified: true,
    verification_summary: "verification report attached",
    expected_duration_us: SHOT_COUNT * 3_000_000,
    measured_duration_us: SHOT_COUNT * 3_000_000,
    selected_shot_count: SHOT_COUNT,
    caption_language: "en",
    caption_cue_count: 12,
    subtitle_mode: "external",
    integrated_loudness_lufs: -16,
    true_peak_dbtp: -1.5,
    warning_codes: [],
    final_video_asset_id: "88888888-2222-4222-8222-000000000010",
    srt_asset_id: "88888888-2222-4222-8222-000000000011",
    webvtt_asset_id: "88888888-2222-4222-8222-000000000012",
    verification_report_asset_id: "88888888-2222-4222-8222-000000000013",
    manifest_asset_id: "88888888-2222-4222-8222-000000000014",
    script_id: "55555555-2222-4222-8222-000000000001",
    script_version: state.scriptVersion,
    storyboard_run_id: "66666666-2222-4222-8222-000000000001",
    narration_run_id: "99999999-2222-4222-8222-000000000002",
    ffmpeg_version: "ffmpeg-test",
    lineage_hash: HASH,
    approval:
      state.approvals > 0
        ? {
            schema_version: "1.0",
            approval_id: "99999999-2222-4222-8222-000000000001",
            render_job_id: "88888888-2222-4222-8222-000000000001",
            approved_by: "local-user",
            approved_at: "2026-08-02T09:00:00Z",
            lineage_hash: HASH,
            applies_to_current_lineage: true,
          }
        : null,
    row_version: 1,
    completed_at: "2026-08-01T11:00:00Z",
  };
}

const PUBLICATION_ID = "aaaaaaaa-2222-4222-8222-000000000001";
const CONNECTION_ID = "bbbbbbbb-2222-4222-8222-000000000001";

function youtubeConnections() {
  return {
    items: [
      {
        connection_id: CONNECTION_ID,
        channel: {
          channel_id: "UCfakevidgenchannel0001",
          title: "VidGen Test Channel",
          thumbnail_url: "https://yt3.example/channel.jpg",
          custom_url: "@vidgen",
        },
        status: "connected",
        granted_scopes: [
          "https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/youtube.readonly",
        ],
        // The envelope key *version* only. There is nowhere here for a token.
        encryption_key_version: "dev-insecure-1",
        credential_expires_at: null,
        last_verified_at: "2026-08-02T09:00:00Z",
        error_code: null,
        created_at: "2026-08-02T09:00:00Z",
        updated_at: "2026-08-02T09:00:00Z",
      },
    ],
    oauth_configured: true,
    production_authentication_available: false,
  };
}

function publicationMetadata() {
  return {
    title: "Season 3 Episode 4 - animated recap",
    description: "An animated recap, generated with VidGen.",
    tags: ["recap", "animated recap"],
    category_id: "24",
    default_language: "en",
    caption_language: "en",
    caption_track_name: "VidGen recap",
    made_for_kids: false,
    contains_synthetic_media: true,
    embeddable: true,
    notify_subscribers: false,
    requested_privacy: "private",
    scheduled_publish_at: null,
  };
}

function publicationProjection() {
  return {
    publication_id: PUBLICATION_ID,
    project_id: PROJECT_ID,
    connection_id: CONNECTION_ID,
    channel_id: "UCfakevidgenchannel0001",
    final_render_asset_id: "88888888-2222-4222-8222-000000000010",
    final_editorial_run_id: "77777777-2222-4222-8222-000000000001",
    approval_id: "99999999-2222-4222-8222-000000000001",
    publication_identity: HASH,
    metadata_version: 1,
    status: "PRIVATE_READY",
    phase: "VERIFICATION",
    video_id: "vidfake000000001",
    video_url: "https://www.youtube.com/watch?v=vidfake000000001",
    total_bytes: 2097152,
    confirmed_offset: 2097152,
    processing_state: "succeeded",
    caption_status: "succeeded",
    caption_track_id: "capfake000000001",
    thumbnail_status: "succeeded",
    requested_privacy: "private",
    // What YouTube reports, never what was requested.
    actual_privacy: "private",
    scheduled_publish_at: null,
    contains_synthetic_media: true,
    made_for_kids: false,
    notify_subscribers: false,
    quota_units: 251,
    capability_profile_version: "youtube-data-v3/2026-08",
    publisher_version: "t25/1.0",
    gate_version: "final-gate/1.0",
    render_identity: HASH,
    metadata: publicationMetadata(),
    failure: null,
    row_version: 1,
    created_at: "2026-08-02T09:00:00Z",
    updated_at: "2026-08-02T09:05:00Z",
  };
}

function publicationCollection(state: FakeApiState) {
  // The gate only opens once the render is approved, exactly as the backend
  // decides it: an unapproved render explains itself rather than hiding.
  const allowed = state.approvals > 0;
  return {
    project_id: PROJECT_ID,
    items: [publicationProjection()],
    gate: {
      project_id: PROJECT_ID,
      allowed,
      final_render_asset_id: "88888888-2222-4222-8222-000000000010",
      final_editorial_run_id: "77777777-2222-4222-8222-000000000001",
      approval_id: allowed ? "99999999-2222-4222-8222-000000000001" : null,
      caption_asset_id: "88888888-2222-4222-8222-000000000011",
      gate_version: "final-gate/1.0",
      failures: allowed
        ? []
        : [
            {
              code: "RENDER_NOT_APPROVED",
              summary: "This render has not been approved in the review UI.",
              retryable: false,
              http_status: null,
              remediation: "Approve the render on the final review page, then publish.",
            },
          ],
      warnings: [],
      row_version: 1,
    },
  };
}

function publicationDetail() {
  return { ...publicationProjection(), assets: [], attempts: [] };
}

function costs() {
  return {
    projectId: PROJECT_ID,
    warningCap: "8.000000",
    hardCap: "20.000000",
    reservedAmount: "1.000000",
    committedAmount: "1.000000",
    releasedAmount: "0",
    remainingAmount: "18.000000",
    warningPercentage: "12.5",
    hardPercentage: "5",
    byProvider: { fake: "1.000000" },
    byModel: { "fake-video": "1.000000" },
    byOperation: { video_generation: "1.000000" },
    byReason: { generation: "1.000000" },
  };
}
