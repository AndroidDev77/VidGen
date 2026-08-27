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
  };
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
    if (path.includes("/shots/") && method === "GET") {
      const id = path.split("/").pop() ?? "";
      const index =
        Array.from({ length: SHOT_COUNT }, (_, value) => value).find(
          (value) => shotId(value) === id,
        ) ?? 0;
      return json(route, shotDetail(state, index));
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
