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
  RepairAttemptProjection,
  RepairCollectionResponse,
  RepairRunDetailProjection,
  RepairRunProjection,
  VisualQACollectionResponse,
  VisualQAEvidenceResponse,
  VisualQARunDetailProjection,
  VisualQARunProjection,
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
  voice_profile_id: uuid(9, 1),
};

/** The voices a fake-provider deployment offers, and the one selected. */
export const voiceProfiles = {
  project_id: PROJECT_ID,
  selected_voice_profile_id: uuid(9, 1),
  items: [
    {
      schema_version: "1.0",
      voice_profile_id: uuid(9, 1),
      project_id: PROJECT_ID,
      provider: "fake",
      provider_voice_id: "vidgen-local-fake-voice",
      model: "fake-tts",
      language: "en",
      profile_version: 1,
      configuration_hash: "b".repeat(64),
      output_format: "wav",
      scope: "project",
      selected: true,
    },
    {
      schema_version: "1.0",
      voice_profile_id: uuid(9, 2),
      project_id: null,
      provider: "fake",
      provider_voice_id: "vidgen-local-fake-alt",
      model: "fake-tts",
      language: "en",
      profile_version: 1,
      configuration_hash: "c".repeat(64),
      output_format: "wav",
      scope: "shared",
      selected: false,
    },
  ],
};

/** One command of each interesting shape: running, waiting and failed. */
export const commands = {
  project_id: PROJECT_ID,
  items: [
    {
      schema_version: "1.0",
      command_id: uuid(8, 1),
      project_id: PROJECT_ID,
      command_type: "reference_build",
      status: "awaiting_review",
      target_type: "project",
      target_id: PROJECT_ID,
      workflow_id: "vidgen-references-1",
      run_id: "vidgen-references-1-run",
      attempt: 1,
      max_attempts: 5,
      progress: {
        schema_version: "1.0",
        phase: "awaiting_review",
        percent: 40,
        waiting_reason: "reference_approval_required",
      },
      result: null,
      failure: null,
      row_version: 3,
      created_at: "2026-08-02T10:00:00Z",
      updated_at: "2026-08-02T10:05:00Z",
      dispatched_at: "2026-08-02T10:01:00Z",
      started_at: "2026-08-02T10:01:00Z",
      completed_at: null,
      permitted_actions: ["cancel"],
    },
    {
      schema_version: "1.0",
      command_id: uuid(8, 2),
      project_id: PROJECT_ID,
      command_type: "shot_regenerate",
      status: "failed",
      target_type: "shot",
      target_id: uuid(3, 1),
      workflow_id: null,
      run_id: null,
      attempt: 3,
      max_attempts: 3,
      progress: { schema_version: "1.0", phase: "failed", percent: 0, waiting_reason: "" },
      result: null,
      failure: {
        schema_version: "1.0",
        code: "command_upstream_stale",
        summary: "The inputs this command was created against have changed.",
        retryable: false,
        attempt: 3,
      },
      row_version: 5,
      created_at: "2026-08-02T09:00:00Z",
      updated_at: "2026-08-02T09:10:00Z",
      dispatched_at: null,
      started_at: null,
      completed_at: "2026-08-02T09:10:00Z",
      permitted_actions: ["retry"],
    },
  ],
  generation_runs: [
    {
      schema_version: "1.0",
      generation_run_id: uuid(7, 1),
      project_id: PROJECT_ID,
      sequence: 1,
      status: "active",
      entry_stage: "upload",
      input_identity: "d".repeat(64),
      workflow_id: `vidgen-project-${PROJECT_ID}`,
      run_id: "run-1",
      origin_command_id: null,
      parent_generation_run_id: null,
      created_at: "2026-08-01T09:00:00Z",
      updated_at: "2026-08-02T10:00:00Z",
    },
  ],
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
  progress_percent: 100,
  checkpoint: "complete",
  attempt_count: 1,
  cancel_requested: false,
  failure_code: null,
  failure_classification: null,
  output_sha256: HASH,
  input_hash: HASH,
  renderer_version: "t17/1",
  downloadable: true,
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


// --- T20 visual QA -----------------------------------------------------------
const QA_DIMENSIONS = [
  ["character_identity", 25],
  ["character_count", 10],
  ["location", 10],
  ["wardrobe_and_state", 10],
  ["action_and_motion", 15],
  ["composition", 10],
  ["anatomy_and_artifacts", 10],
  ["continuity_and_style", 10],
] as const;

export function visualQaRun(
  index: number,
  overrides: Partial<VisualQARunProjection> = {},
): VisualQARunProjection {
  return {
    qa_run_id: uuid(700 + index, 9),
    project_id: PROJECT_ID,
    shot_id: uuid(index, 6),
    target_type: "video",
    status: "visual_qa_complete",
    outcome: "FAIL",
    score: 82.5,
    pass_threshold: 85,
    importance: "normal",
    hard_failure: true,
    repair_recommendation: "NEW_SEED",
    repair_codes: ["WRONG_CHARACTER_IDENTITY"],
    warning_codes: ["excessive_freeze"],
    confidence: 0.91,
    adjudicated: true,
    human_review_decision: null,
    provider: "fake",
    model: "fake-visual-qa/1",
    cost_microusd: 24_000,
    rubric_version: "visual-qa-rubric/1.0",
    threshold_version: "visual-qa-thresholds/1.0",
    sampling_version: "visual-qa-sampler/1.0",
    sample_count: 12,
    deterministic_warning_count: 2,
    row_version: 3,
    created_at: "2026-08-02T11:00:00Z",
    completed_at: "2026-08-02T11:01:00Z",
    ...overrides,
  };
}

export function visualQaDetail(
  index: number,
  overrides: Partial<VisualQARunDetailProjection> = {},
): VisualQARunDetailProjection {
  return {
    ...visualQaRun(index),
    dimensions: QA_DIMENSIONS.map(([dimension, weight]) => ({
      dimension,
      applicable: true,
      raw_score: dimension === "character_identity" ? 40 : 95,
      weight,
      effective_weight: weight,
      weighted_contribution:
        ((dimension === "character_identity" ? 40 : 95) * weight) / 100,
      confidence: 0.9,
      warning_codes: [],
      hard_failure_codes: dimension === "character_identity" ? ["WRONG_CHARACTER_IDENTITY"] : [],
      repair_codes: dimension === "character_identity" ? ["WRONG_CHARACTER_IDENTITY"] : [],
      finding_summaries:
        dimension === "character_identity"
          ? ["The subject does not match the approved identity reference."]
          : [],
    })),
    diagnostics: [
      {
        code: "freeze_ratio",
        outcome: "warning",
        diagnostic_code: "excessive_freeze",
        measurement: 0.51,
        threshold: 0.35,
        evidence_timestamp_us: 1_500_000,
        repair_code: "EXCESSIVE_FREEZE",
        message: "",
      },
    ],
    samples: [
      {
        sample_id: uuid(800 + index, 9),
        sequence: 0,
        sample_type: "first_frame",
        requested_timestamp_us: 0,
        actual_timestamp_us: 0,
        shot_relative_timestamp_us: 0,
        frame_asset_id: uuid(900 + index, 6),
        frame_sha256: HASH,
        selection_reason: "first decodable frame",
        contact_sheet_position: 0,
      },
    ],
    compared_reference_asset_ids: [uuid(950 + index, 6)],
    contact_sheet_asset_id: uuid(960 + index, 6),
    report_asset_id: uuid(970 + index, 6),
    adjudication: {
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
    },
    ...overrides,
  };
}

export function visualQaCollection(index: number): VisualQACollectionResponse {
  return {
    project_id: PROJECT_ID,
    items: [
      visualQaRun(index, {
        qa_run_id: uuid(600 + index, 9),
        target_type: "keyframe",
        outcome: "PASS",
        score: 96,
        hard_failure: false,
        repair_codes: [],
        repair_recommendation: "NONE",
      }),
      visualQaRun(index),
    ],
  };
}

export function visualQaEvidence(index: number): VisualQAEvidenceResponse {
  const detail = visualQaDetail(index);
  return {
    qa_run_id: detail.qa_run_id,
    items: [
      {
        evidence_id: uuid(1000 + index, 9),
        finding_id: uuid(1100 + index, 9),
        evidence_type: "reference_comparison",
        sample_id: detail.samples[0]!.sample_id,
        frame_asset_id: detail.samples[0]!.frame_asset_id,
        shot_relative_timestamp_us: 1_500_000,
        source_relative_timestamp_us: 1_500_000,
        contact_sheet_position: 0,
        bounding_box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
        compared_reference_asset_id: detail.compared_reference_asset_ids[0]!,
        confidence: 0.93,
        explanation: "Face geometry does not match the approved identity version.",
      },
    ],
    samples: detail.samples,
  };
}


/* --- T21 repair and fallback ---------------------------------------------- */

export function repairRun(
  index: number,
  overrides: Partial<RepairRunProjection> = {},
): RepairRunProjection {
  return {
    repair_run_id: uuid(1100 + index, 9),
    project_id: PROJECT_ID,
    shot_id: uuid(index, 6),
    state: "HUMAN_REVIEW_REQUIRED",
    root_animation_attempt_id: uuid(1200 + index, 6),
    triggering_qa_result_id: uuid(700 + index, 9),
    failure_category: "prompt_issue",
    failure_severity: "structural",
    repair_code: "wrong_character_identity",
    qa_score: 62.5,
    pass_threshold: 85,
    hard_failure: true,
    hard_failure_reason: "WRONG_CHARACTER_IDENTITY",
    total_attempt_count: 5,
    same_provider_repairs_used: 2,
    alternate_provider_attempts_used: 1,
    fallback_renders_used: 1,
    selected_attempt_id: null,
    selected_asset_id: null,
    final_qa_result_id: null,
    final_qa_score: null,
    human_review_reason: "attempt_limit_reached",
    human_review_resolved: false,
    policy_version: "t21-repair-policy/1.0",
    planner_version: "t21-repair-planner-deterministic/1.0",
    row_version: 3,
    created_at: "2026-08-02T11:05:00Z",
    updated_at: "2026-08-02T11:20:00Z",
    ...overrides,
  };
}

function repairAttempt(
  index: number,
  ordinal: number,
  kind: RepairAttemptProjection["attempt_kind"],
  overrides: Partial<RepairAttemptProjection> = {},
): RepairAttemptProjection {
  return {
    attempt_id: uuid(1300 + index * 10 + ordinal, 9),
    attempt_ordinal: ordinal,
    attempt_kind: kind,
    status: "failed",
    predecessor_attempt_id: ordinal === 0 ? null : uuid(1300 + index * 10 + ordinal - 1, 9),
    root_animation_attempt_id: uuid(1200 + index, 6),
    provider: kind === "alternate_provider" ? "google_veo" : "fake",
    model: kind === "alternate_provider" ? "veo-3.1-fast-generate-001" : "gen4_turbo",
    provider_operation_id: kind === "alternate_provider" ? "operations/abc123" : null,
    capability_profile_hash: null,
    prompt_hash: ordinal === 0 ? null : HASH,
    prompt_delta:
      ordinal === 1
        ? {
            planner_version: "t21-repair-planner-deterministic/1.0",
            repair_reason: "repair wrong_character_identity",
            added_clauses: ["Match the referenced character's face, hair and skin tone exactly."],
            removed_clauses: [],
            rewritten_clauses: [],
            preserved_constraint_ids: ["character-identity-0", "location", "timing"],
            touched_constraint_ids: [],
            before_prompt_hash: HASH,
            after_prompt_hash: HASH,
            seed_changed: true,
            previous_seed: null,
            new_seed: 12345,
          }
        : null,
    seed: ordinal === 0 ? null : 12345,
    output_asset_ids: [],
    output_qa_result_id: null,
    qa_score: 62.5,
    qa_outcome: "FAIL",
    estimated_cost: "0.200000",
    actual_cost: "0.200000",
    currency: "USD",
    failure_category: "prompt_issue",
    failure_code: null,
    selected: false,
    created_at: "2026-08-02T11:06:00Z",
    completed_at: "2026-08-02T11:08:00Z",
    ...overrides,
  };
}

export function repairDetail(
  index: number,
  overrides: Partial<RepairRunDetailProjection> = {},
): RepairRunDetailProjection {
  return {
    ...repairRun(index),
    attempts: [
      repairAttempt(index, 0, "original"),
      repairAttempt(index, 1, "same_provider_repair"),
      repairAttempt(index, 2, "same_provider_repair"),
      repairAttempt(index, 3, "alternate_provider"),
      repairAttempt(index, 4, "deterministic_fallback", {
        provider: "parallax",
        model: "parallax-renderer/1.0",
        estimated_cost: "0.000000",
        actual_cost: "0.000000",
      }),
    ],
    decisions: [
      {
        decision_id: uuid(1400 + index, 9),
        sequence: 0,
        route: "same_provider_repair",
        rationale: ["same-provider repair 1 of 2"],
        failure_category: "prompt_issue",
        repair_codes: ["WRONG_CHARACTER_IDENTITY"],
        human_review_reason: null,
        estimated_next_cost: "0.200000",
        budget_remaining: "9.000000",
        planner_version: "t21-repair-planner-deterministic/1.0",
        policy_version: "t21-repair-policy/1.0",
        created_at: "2026-08-02T11:06:00Z",
      },
    ],
    fallback: {
      repair_attempt_id: uuid(1300 + index * 10 + 4, 9),
      renderer_version: "parallax-renderer/1.0",
      render_identity: HASH,
      input_asset_ids: [uuid(1500 + index, 6)],
      exact_duration_us: 3_000_000,
      width: 1280,
      height: 720,
      frame_rate: "24/1",
      pixel_format: "yuv420p",
      video_codec: "h264",
      output_asset_id: uuid(1600 + index, 6),
      manifest_asset_id: uuid(1700 + index, 6),
      qa_result_id: null,
    },
    budget: {
      currency: "USD",
      total_repair_cost: "0.800000",
      estimated_repair_cost: "0.800000",
      per_shot_repair_cost_limit: null,
      project_hard_cap: "10.000000",
      project_remaining: "9.200000",
    },
    ...overrides,
  };
}

export function repairCollection(index: number): RepairCollectionResponse {
  return { project_id: PROJECT_ID, items: [repairRun(index)] };
}
