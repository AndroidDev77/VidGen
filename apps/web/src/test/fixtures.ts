import type {
  PipelineFailureListResponse,
  ProjectCostSummaryResponse,
  ProjectEventProjection,
  ProviderAttemptListResponse,
  RenderProjection,
  ScriptProjection,
  ScriptSegmentProjection,
  ShotAttemptProjection,
  ShotDetailProjection,
  StageTimelineEntry,
  StoryboardProjection,
  StoryboardShotProjection,
  TranscriptProjection,
  WorkflowStatusProjection,
} from "@vidgen/contracts";
import { PIPELINE_STAGE_ORDER } from "@vidgen/contracts";

import type { ProjectListItem } from "../api/projects";

export const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
export const SHOT_COUNT = 10;
const HASH = "a".repeat(64);

export function uuid(index: number, kind = 2): string {
  const suffix = String(index).padStart(12, "0");
  return `${String(kind).repeat(8)}-2222-4222-8222-${suffix}`;
}

export const projectListItem: ProjectListItem = {
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
  has_failures: true,
  row_version: 1,
};

export const projectDetail = {
  id: PROJECT_ID,
  name: "Season 3 Episode 4",
  status: "review",
  target_duration_seconds: 300,
  visual_style: "flat editorial cartoon",
  humor_intensity: 6,
  created_at: "2026-08-01T09:00:00Z",
  updated_at: "2026-08-02T10:30:00Z",
};

export const projectStatus = {
  project_id: PROJECT_ID,
  status: "review",
  source_video_id: uuid(1, 3),
  source_asset_id: uuid(2, 3),
  upload_status: "completed",
  error_code: null,
};

const stages: StageTimelineEntry[] = PIPELINE_STAGE_ORDER.map((stage) => ({
  schema_version: "1.0",
  stage,
  state: "complete",
  started_at: null,
  completed_at: null,
  detail_code: null,
}));

export const workflowStatus: WorkflowStatusProjection = {
  schema_version: "1.0",
  project_id: PROJECT_ID,
  workflow_id: `vidgen-project-${PROJECT_ID}`,
  run_id: `vidgen-project-${PROJECT_ID}-run`,
  status: "review",
  current_stage: "review",
  completed_stages: [...PIPELINE_STAGE_ORDER],
  cancelled: false,
  started_at: "2026-08-01T09:05:00Z",
  updated_at: "2026-08-01T10:05:00Z",
  elapsed_seconds: 3600,
  total_shot_count: SHOT_COUNT,
  completed_shot_count: SHOT_COUNT,
  failed_shot_count: 0,
  retryable_failure_count: 1,
  render_status: "render_complete",
  stages,
  progress_percentage: 100,
};

export const transcript: TranscriptProjection = {
  schema_version: "1.0",
  transcript_id: uuid(1, 4),
  project_id: PROJECT_ID,
  version: 1,
  language: "en",
  origin: "transcription",
  duration_seconds: 1800,
  coverage_score: 0.98,
  selected: true,
  row_version: 3,
  source_asset_id: uuid(2, 4),
  segments: [
    {
      schema_version: "1.0",
      segment_id: uuid(10, 4),
      sequence: 0,
      start_seconds: 0,
      end_seconds: 9,
      text: "The detective explains the plan.",
      speaker_label: "SPEAKER_00",
      confidence: 0.94,
      edited: false,
      row_version: 1,
    },
    {
      schema_version: "1.0",
      segment_id: uuid(11, 4),
      sequence: 1,
      start_seconds: 10,
      end_seconds: 19,
      text: "The suspect denies everything loudly.",
      speaker_label: "SPEAKER_01",
      confidence: 0.71,
      edited: false,
      row_version: 1,
    },
  ],
};

function scriptSegment(index: number): ScriptSegmentProjection {
  return {
    schema_version: "1.0",
    segment_id: uuid(20 + index, 5),
    stable_segment_id: uuid(100 + index, 5),
    sequence: index,
    segment_type: "narration",
    speaker_kind: "narrator",
    speaker_label: null,
    text: `Recap beat number ${index + 1} lands with a joke.`,
    visual_gag: null,
    joke_annotation_count: 1,
    plot_beat_ids: [],
    word_count: 8,
    estimated_duration_ms: 3000,
    measured_narration_duration_ms: 3000,
    locked: false,
    content_hash: HASH,
    row_version: 1,
  };
}

export const script: ScriptProjection = {
  schema_version: "1.0",
  project_id: PROJECT_ID,
  script: {
    schema_version: "1.0",
    script_id: uuid(1, 5),
    version: 1,
    status: "approved",
    selected: true,
    actual_word_count: 80,
    target_word_count: 700,
    target_duration_ms: 300_000,
    parent_script_id: null,
    created_at: "2026-08-01T09:30:00Z",
    row_version: 2,
  },
  approved: true,
  segments: Array.from({ length: SHOT_COUNT }, (_, index) => scriptSegment(index)),
};

export function storyboardShot(index: number): StoryboardShotProjection {
  return {
    schema_version: "1.0",
    shot_id: uuid(index, 6),
    stable_shot_id: uuid(100 + index, 6),
    global_sequence: index,
    segment_sequence: 0,
    script_segment_id: uuid(20 + index, 5),
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
    selected_keyframe_asset_id: uuid(200 + index, 6),
    selected_video_asset_id: uuid(300 + index, 6),
    provider: "fake",
    model: "fake-video",
    attempt_count: 1,
    cost_amount: "0.100000",
    warning_code: null,
    failure_code: null,
    row_version: 1,
  };
}

export const storyboard: StoryboardProjection = {
  schema_version: "1.0",
  project_id: PROJECT_ID,
  storyboard_run_id: uuid(1, 6),
  version: 1,
  selected: true,
  shot_count: SHOT_COUNT,
  segment_count: SHOT_COUNT,
  total_duration_us: SHOT_COUNT * 3_000_000,
  timing_manifest_asset_id: uuid(2, 6),
  row_version: 1,
  shots: Array.from({ length: SHOT_COUNT }, (_, index) => storyboardShot(index)),
};

function videoAttempt(index: number): ShotAttemptProjection {
  return {
    schema_version: "1.0",
    attempt_id: uuid(400 + index, 7),
    kind: "video",
    attempt_number: 1,
    status: "succeeded",
    asset_id: uuid(300 + index, 6),
    provider: "fake",
    model: "fake-video",
    provider_task_id: `fake-task-${index}`,
    generation_identity: HASH,
    prompt_version: "v1",
    generated_duration_us: 4_000_000,
    usable_duration_us: 3_000_000,
    cost_amount: "0.100000",
    failure_class: null,
    selected: true,
    created_at: "2026-08-01T10:00:00Z",
  };
}

export function shotDetail(index: number): ShotDetailProjection {
  return {
    schema_version: "1.0",
    shot: storyboardShot(index),
    child_workflow_id: `vidgen-shot-${uuid(index, 6)}`,
    child_workflow_status: "locked",
    child_workflow_retryable: false,
    identity_hash: HASH,
    trim_instructions_asset_id: null,
    source_evidence_ids: [uuid(2, 3)],
    keyframe_attempts: [
      {
        schema_version: "1.0",
        attempt_id: uuid(500 + index, 7),
        kind: "keyframe",
        attempt_number: 1,
        status: "succeeded",
        asset_id: uuid(200 + index, 6),
        provider: "fake",
        model: "fake-image",
        provider_task_id: null,
        generation_identity: HASH,
        prompt_version: "v1",
        generated_duration_us: null,
        usable_duration_us: null,
        cost_amount: null,
        failure_class: null,
        selected: true,
        created_at: "2026-08-01T09:59:00Z",
      },
    ],
    video_attempts: [videoAttempt(index)],
    regeneration_history: [],
  };
}

export const render: RenderProjection = {
  schema_version: "1.0",
  render_job_id: uuid(1, 8),
  project_id: PROJECT_ID,
  status: "render_complete",
  attempt: 1,
  render_version: "t17/1",
  render_identity: HASH,
  selected: true,
  stale: false,
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
  final_video_asset_id: uuid(10, 8),
  srt_asset_id: uuid(11, 8),
  webvtt_asset_id: uuid(12, 8),
  verification_report_asset_id: uuid(13, 8),
  manifest_asset_id: uuid(14, 8),
  script_id: uuid(1, 5),
  script_version: 1,
  storyboard_run_id: uuid(1, 6),
  narration_run_id: uuid(1, 9),
  ffmpeg_version: "ffmpeg-test",
  lineage_hash: HASH,
  approval: null,
  row_version: 4,
  completed_at: "2026-08-01T11:00:00Z",
};

export const costs: ProjectCostSummaryResponse = {
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

export const providerAttempts: ProviderAttemptListResponse = {
  total: 1,
  offset: 0,
  limit: 10,
  items: [
    {
      id: uuid(1, 7),
      provider: "fake",
      model: "fake-video",
      operation: "video_generation",
      status: "succeeded",
      failureClass: null,
      latencyMs: 1200,
      startedAt: "2026-08-01T10:00:00Z",
    },
  ],
};

export const failures: PipelineFailureListResponse = {
  items: [
    {
      id: uuid(2, 7),
      workflowId: `vidgen-project-${PROJECT_ID}`,
      stage: "animation",
      failureClass: "transient",
      errorCode: "provider_timeout",
      retryable: true,
      status: "recovered",
    },
  ],
};

export function projectEvent(
  eventId: number,
  overrides: Partial<ProjectEventProjection> = {},
): ProjectEventProjection {
  return {
    schema_version: "1.0",
    event_id: eventId,
    project_id: PROJECT_ID,
    workflow_id: `vidgen-project-${PROJECT_ID}`,
    event_type: "workflow_started",
    stage: "upload",
    status: "running",
    progress_percentage: null,
    completed_shot_count: null,
    total_shot_count: null,
    retryable_failure_count: null,
    render_status: null,
    cost_summary_version: null,
    warning_code: null,
    failure_code: null,
    created_at: "2026-08-01T09:05:00Z",
    ...overrides,
  };
}
