export type UUID = string;
export interface ContinuityEvidenceLink { schema_version: "1.0"; evidence_id: UUID; scene_id?: UUID | null; source_timestamp_ms?: number | null }
export interface ContinuityInterval { schema_version: "1.0"; start_sequence: number; end_sequence?: number | null }
export interface CharacterIdentityBible { schema_version: "1.0"; character_id: UUID; display_name: string; anonymous_speaker_label?: string | null; aliases: string[]; role?: string | null; stable_traits: Record<string, string | string[] | null>; evidence: ContinuityEvidenceLink[]; confidence: number; ambiguities: Array<{ field: string; alternatives: string[] }> }
export interface LocationIdentityBible { schema_version: "1.0"; location_id: UUID; display_name: string; location_type?: string | null; stable_traits: Record<string, string | string[] | null>; evidence: ContinuityEvidenceLink[]; confidence: number; ambiguities: Array<{ field: string; alternatives: string[] }> }
export interface CharacterAppearanceStateV1 { schema_version: "1.0"; interval: ContinuityInterval; wardrobe: string[]; hairstyle?: string | null; injuries: string[]; carried_props: string[]; emotional_state?: string | null; action_state?: string | null; evidence: ContinuityEvidenceLink[]; confidence: number; unresolved_conflicts: string[] }
export interface LocationEnvironmentState { schema_version: "1.0"; interval: ContinuityInterval; time_of_day?: string | null; weather?: string | null; lighting?: string | null; damage: string[]; crowd_state?: string | null; evidence: ContinuityEvidenceLink[]; confidence: number; conflicts: string[] }
export interface ShotReferenceBundle { schema_version: "1.0"; id: UUID; project_id: UUID; storyboard_run_id: UUID; shot_id: UUID; shot_sequence: number; character_identity_version_ids: UUID[]; character_state_snapshot_ids: UUID[]; location_identity_version_id?: UUID | null; location_state_snapshot_id?: UUID | null; references: Array<{ asset_id: UUID; sha256: string; role: string; entity_id: UUID; required: boolean; priority: number }>; required_props: string[]; continuity_warnings: string[]; omitted_references: string[]; provider_reference_limit: number; bundle_hash: string; resolver_version: string; created_at: string }
export interface VoiceProfile { schema_version: "1.0"; voice_profile_id: UUID; project_id?: UUID | null; account_scope?: string | null; provider: string; provider_voice_id: string; model: string; language: string; default_speaking_instructions: string; default_pace: number; pronunciation_dictionary: Record<string,string>; output_format: string; sample_rate_hz: number; channels: 1 | 2; profile_version: number; configuration_hash: string; created_at: string; updated_at: string }
export interface NarrationWordTiming { schema_version: "1.0"; word_index: number; word: string; comparison_token: string; punctuation: string; start_seconds: number; end_seconds: number; confidence: number }
export interface NarrationAlignment { schema_version: "1.0"; timings: NarrationWordTiming[]; coverage: number; insertions: string[]; omissions: string[]; substitutions: string[]; diagnostics: string[] }
export interface NarrationQualityDiagnostic { schema_version: "1.0"; code: string; severity: "warning" | "error"; message: string; measured_value?: number | null; threshold?: number | null }
export interface NarrationQualityReport { schema_version: "1.0"; valid: boolean; diagnostics: NarrationQualityDiagnostic[]; clipping_ratio: number; leading_silence_seconds: number; trailing_silence_seconds: number; speaking_rate_wpm: number; alignment_coverage: number }

export type FailureClass =
  | "transient"
  | "permanent"
  | "validation"
  | "quota"
  | "provider"
  | "cancelled";

export interface WorkflowFailure {
  schema_version: "1.0";
  error_class: FailureClass;
  code: string;
  message: string;
  retryable: boolean;
  details: Record<string, unknown>;
}

export interface ProjectWorkflowInput {
  schema_version: "1.0";
  project_id: UUID;
  source_video_id: UUID;
  idempotency_key: string;
  provider_configuration_version: string;
  trace_context: Record<string,string>;
}

export interface AnimationActivityInput {
  schema_version: "1.0";
  project_id: UUID;
  storyboard_id: UUID | null;
  image_generation_run_id: UUID | null;
  animation_run_id: UUID;
  provider_configuration_version: string;
  idempotency_key: string;
  trace_context: Record<string,string>;
}

export interface ProjectWorkflowState {
  schema_version: "1.0";
  project_id: UUID;
  status: string;
  completed_stages: string[];
  cancelled: boolean;
  failure: WorkflowFailure | null;
  updated_at: string | null;
}

export interface SourceTimeRange { start_seconds: number; end_seconds: number }
export interface EvidenceTranscriptItem {
  source_range: SourceTimeRange;
  source_asset_id: UUID;
  text: string;
  speaker_label: string | null;
  confidence: number | null;
  segment_sequence: number;
}
export interface SceneEvidence {
  schema_version: "1.0";
  scene_sequence: number;
  source_range: SourceTimeRange;
  source_video_asset_id: UUID;
  source_audio_asset_id: UUID | null;
  representative_frame_asset_ids: UUID[];
  representative_frame_timestamps: number[];
  transcript_items: EvidenceTranscriptItem[];
}
export interface EvidenceProvenance {
  transcript_origin: "subtitle" | "audio_transcription";
  transcript_id: UUID;
  transcript_asset_id: UUID;
  subtitle_asset_id: UUID | null;
  input_hash: string;
  builder_version: string;
  generation_parameters: Record<string, unknown>;
}
export interface EvidenceDiagnostic {
  code: string;
  severity: "warning" | "error";
  message: string;
  scene_sequence: number | null;
}
export interface EvidencePackage {
  schema_version: "1.0";
  package_id: UUID;
  project_id: UUID;
  version: number;
  source_video_id: UUID;
  source_video_asset_id: UUID;
  contact_sheet_asset_id: UUID | null;
  scenes: SceneEvidence[];
  provenance: EvidenceProvenance;
  diagnostics: EvidenceDiagnostic[];
}

export type AssetKind =
  | "source_video"
  | "frame"
  | "image"
  | "video"
  | "audio"
  | "subtitle"
  | "render"
  | "thumbnail"
  | "json";

export interface AssetRef {
  asset_id: UUID;
  kind: AssetKind;
  sha256: string;
  uri: string;
  media_type: string;
  created_at: string;
}

export interface CharacterState {
  character_id: UUID;
  wardrobe_state: string;
  injury_state: string;
  emotional_state: string;
  location_id: UUID;
  scene_id: UUID;
  props: string[];
}

// T14 image generation. Provider image bytes are deliberately absent.
export type KeyframeRole = "FIRST_FRAME" | "LAST_FRAME";
export type ImageQuality = "low" | "medium" | "high";
export type ImageFormat = "png" | "jpeg" | "webp";
export interface ImageReferenceBinding { schema_version: "1.0"; asset_id: UUID; sha256: string; semantic_role: "source" | "style" | "character" | "location" | "approved"; required: boolean; order: number; media_type: "image/png" | "image/jpeg" | "image/webp" }
export interface VisualIntent { schema_version: "1.0"; shot_id: UUID; shot_sequence: number; keyframe_role: KeyframeRole; visual_purpose: string; style_lock: string; visible_character_count: number; character_descriptions: string[]; character_states: string[]; location_description: string; location_invariants: string[]; props_and_ownership: string[]; composition: string; shot_size: string; camera_angle: string; subject_priority: string[]; pose: string; primary_action: string; emotional_state: string; continuity_assumptions: string[]; required_source_evidence: UUID[]; positive_constraints: string[]; negative_constraints: string[]; warnings: string[] }
export interface ImagePromptPackage { schema_version: "1.0"; visual_intent: VisualIntent; prompt: string; prompt_compiler_version: string; template_version: string; references: ImageReferenceBinding[]; diagnostics: string[]; prompt_hash: string; input_hash: string; provider_parameters: Record<string, unknown> }
export interface ImageValidationDiagnostic { schema_version: "1.0"; code: string; severity: "error" | "warning"; message: string }
export interface ImageValidationReport { schema_version: "1.0"; valid: boolean; actual_format: ImageFormat | null; mime_type: string | null; width: number | null; height: number | null; aspect_ratio: number | null; color_mode: string | null; has_alpha: boolean; byte_size: number; sha256: string | null; diagnostics: ImageValidationDiagnostic[] }
export interface GeneratedImageCandidate { schema_version: "1.0"; generated_image_id: UUID; asset_id: UUID; shot_id: UUID; keyframe_role: KeyframeRole; selected: boolean; validation: ImageValidationReport }
export interface ShotKeyframeResult { schema_version: "1.0"; shot_id: UUID; keyframe_role: KeyframeRole; status: "completed" | "reused" | "failed"; prompt_hash: string; candidate: GeneratedImageCandidate | null; error_code: string | null }
export interface ImageGenerationRunRequest { schema_version: "1.0"; project_id: UUID; storyboard_id: UUID | null; idempotency_key: string; provider_configuration_version: string; shot_id: UUID | null; keyframe_role: KeyframeRole | null }
export interface ImageGenerationRunResult { schema_version: "1.0"; run_id: UUID; storyboard_id: UUID; storyboard_version: number; requested_count: number; completed_count: number; reused_count: number; failed_count: number; status: string }
export interface ImageGenerationResult extends ImageGenerationRunResult { items: ShotKeyframeResult[] }

export type ShotWorkflowStatus = "defined" | "prompting" | "keyframe_generating" | "keyframe_qa" | "animating" | "video_qa" | "locked" | "failed" | "cancelled";
export interface ShotWorkflowFailure { schema_version: "1.0"; classification: string; code: string; retryable: boolean; attempt: number; message: string }
export interface ShotWorkflowIdentity { schema_version: "1.0"; project_id: UUID; storyboard_run_id: UUID; storyboard_input_hash: string; storyboard_shot_id: UUID; canonical_shot_hash: string; shot_sequence: number; timing_manifest_hash: string; t14_configuration_identity: string; t15_capability_profile_identity: string; t14_pipeline_version: string; t15_pipeline_version: string; t16_workflow_version: "t16/1"; attempt_policy_version: "shot-attempt/1"; identity_hash: string }
export interface ShotWorkflowInput { schema_version: "1.0"; project_id: UUID; storyboard_run_id: UUID; storyboard_shot_id: UUID; shot_input_hash: string; workflow_identity: ShotWorkflowIdentity; t14_run_id: UUID | null; t15_run_id: UUID | null; parent_workflow_id: string | null; idempotency_key: string; trace_context: Record<string, string>; attempt_policy_version: "shot-attempt/1" }
export interface ShotWorkflowProgress { schema_version: "1.0"; state: ShotWorkflowStatus; current_stage: string; current_attempt: number; retryable: boolean; t14_run_id: UUID | null; t15_run_id: UUID | null; selected_keyframe_asset_id: UUID | null; selected_video_asset_id: UUID | null; last_failure: ShotWorkflowFailure | null; last_checkpoint: string | null; started_at: string | null; updated_at: string | null; cost_microusd: number; warning_codes: string[] }
export interface ShotWorkflowCommand { schema_version: "1.0"; command_id: string; project_id: UUID; storyboard_shot_id: UUID; command: "inspect" | "resume" | "retry" | "cancel" | "regenerate" | "outputs"; expected_state: ShotWorkflowStatus | null; new_shot_input_hash: string | null }
export interface ShotWorkflowCommandResult { schema_version: "1.0"; command_id: string; accepted: boolean; state: ShotWorkflowStatus; code: string }
export interface ShotWorkflowQueryResult { schema_version: "1.0"; workflow_id: string; identity_hash: string; progress: ShotWorkflowProgress }
export interface ShotWorkflowResult { schema_version: "1.0"; shot_id: UUID; child_workflow_id: string; identity_hash: string; final_state: ShotWorkflowStatus; t14_run_id: UUID | null; selected_keyframe_asset_id: UUID | null; t15_run_id: UUID | null; selected_video_asset_id: UUID | null; exact_usable_duration_us: number | null; provider_generation_duration_us: number | null; trim_instructions_asset_id: UUID | null; failure: ShotWorkflowFailure | null; warning_codes: string[] }
export interface ProjectShotFanoutInput { schema_version: "1.0"; project_id: UUID; storyboard_run_id: UUID; idempotency_key: string; concurrency: number; trace_context: Record<string, string>; t14_configuration_identity: string; t15_capability_profile_identity: string; attempt_policy_version: "shot-attempt/1" }
export interface ProjectShotFanoutResult { schema_version: "1.0"; project_id: UUID; storyboard_run_id: UUID; status: "shot_generation_queued" | "shot_generation_running" | "shot_generation_partial" | "shot_generation_retrying" | "shot_generation_complete" | "shot_generation_failed" | "shot_generation_cancelled"; results: ShotWorkflowResult[]; total_count: number; queued_count: number; active_count: number; locked_count: number; retryable_failure_count: number; terminal_failure_count: number; cancelled_count: number; current_concurrency: number }

// ---------------------------------------------------------------------------
// T13 storyboard generation and deterministic timing.
// Canonical timing is exact integer microseconds; never a floating-point second.
// ---------------------------------------------------------------------------

export type CameraFraming =
  | "extreme_wide"
  | "wide"
  | "medium_wide"
  | "medium"
  | "medium_close"
  | "close_up"
  | "extreme_close_up"
  | "insert";

export type CameraAngle =
  | "eye_level"
  | "low_angle"
  | "high_angle"
  | "overhead"
  | "dutch"
  | "over_the_shoulder"
  | "point_of_view";

export type CameraMovement =
  | "static"
  | "pan_left"
  | "pan_right"
  | "tilt_up"
  | "tilt_down"
  | "dolly_in"
  | "dolly_out"
  | "tracking"
  | "crane"
  | "handheld"
  | "zoom_in"
  | "zoom_out";

export type MovementIntensity = "none" | "subtle" | "moderate" | "strong";
export type BeatIntent = "establish" | "react" | "reveal" | "punchline" | "continue" | "insert";
export type TransitionKind =
  | "cut"
  | "dissolve"
  | "fade_in"
  | "fade_out"
  | "wipe"
  | "match_cut"
  | "whip_pan";
export type TimeOfDay =
  | "dawn"
  | "morning"
  | "midday"
  | "afternoon"
  | "dusk"
  | "night"
  | "unspecified";
export type ScreenPosition =
  | "left"
  | "center_left"
  | "center"
  | "center_right"
  | "right"
  | "offscreen";
export type ScreenDirection = "neutral" | "left_to_right" | "right_to_left";
export type TrimmingPolicy = "trim_end" | "trim_start" | "trim_center" | "none";
export type BoundaryKind = "sentence" | "clause" | "beat" | "word";

export type StoryboardReferenceType =
  | "evidence_package"
  | "scene_evidence"
  | "script_segment"
  | "narration_segment"
  | "narration_asset"
  | "plot_beat"
  | "character"
  | "location"
  | "episode_model";

export type StoryboardDiagnosticCode =
  | "narration_coverage_gap"
  | "invalid_overlap"
  | "impossible_duration_allocation"
  | "unsupported_provider_duration"
  | "excessive_character_count"
  | "too_many_references"
  | "missing_continuity_state"
  | "invalid_character_reference"
  | "invalid_location_reference"
  | "missing_evidence_reference"
  | "provider_schema_failure"
  | "continuity_contradiction"
  | "unsupported_camera_movement"
  | "unsupported_transition"
  | "nonpositive_duration"
  | "word_range_gap";

export type TimingAdjustmentKind =
  | "boundary_snap"
  | "split"
  | "merge"
  | "clamp_min"
  | "clamp_max"
  | "residual_allocation"
  | "final_end_snap"
  | "generation_round_up"
  | "trim";

export interface VisualProviderCapability {
  schema_version: "1.0";
  capability_profile_id: string;
  profile_version: number;
  provider: string;
  model_family: string;
  /** Empty means continuous durations on `duration_increment_us` steps. */
  supported_generation_durations_us: number[];
  min_generation_duration_us: number;
  max_generation_duration_us: number;
  duration_increment_us: number;
  supported_aspect_ratios: string[];
  supported_resolutions: string[];
  max_characters_per_shot: number;
  max_reference_images: number;
  supports_camera_motion: boolean;
  supported_camera_movements: CameraMovement[];
  supported_transitions: TransitionKind[];
  supports_image_to_video: boolean;
  supports_text_to_video: boolean;
  supports_continuity_seed: boolean;
  trimming_policy: TrimmingPolicy;
  capability_hash: string;
}

export interface CameraPlan {
  schema_version: "1.0";
  framing: CameraFraming;
  angle: CameraAngle;
  movement: CameraMovement;
  movement_intensity: MovementIntensity;
  lens_note: string;
}

export interface ActionPlan {
  schema_version: "1.0";
  subject_action: string;
  secondary_action: string;
  beat_intent: BeatIntent;
  staging_note: string;
  prop_references: string[];
}

export interface TransitionPlan {
  schema_version: "1.0";
  kind: TransitionKind;
  duration_us: number;
  /** Extra generated material for the transition; never narration coverage. */
  handle_us: number;
  note: string;
}

export interface CharacterAppearanceState {
  schema_version: "1.0";
  character_id: UUID;
  appearance_state_id: string;
  wardrobe_state: string;
  injury_state: string;
  emotional_state: string;
}

export interface PropState {
  schema_version: "1.0";
  prop_id: string;
  owner_character_id: UUID | null;
  note: string;
}

export interface SubjectPosition {
  schema_version: "1.0";
  character_id: UUID;
  screen_position: ScreenPosition;
  facing: ScreenDirection;
}

export interface ContinuityState {
  schema_version: "1.0";
  present_character_ids: UUID[];
  character_appearance_states: CharacterAppearanceState[];
  location_id: UUID | null;
  sub_location: string;
  time_of_day: TimeOfDay;
  props: PropState[];
  subject_positions: SubjectPosition[];
  screen_direction: ScreenDirection;
  emotional_state: string;
  environment_conditions: string[];
  previous_shot_id: UUID | null;
  unresolved_warnings: StructuredNote[];
}

export interface StoryboardSourceReference {
  schema_version: "1.0";
  reference_type: StoryboardReferenceType;
  reference_id: UUID;
  start_us: number | null;
  end_us: number | null;
  note: string;
}

export interface NarrationBoundary {
  schema_version: "1.0";
  word_index: number;
  offset_us: number;
  kind: BoundaryKind;
  label: string;
}

export interface StoryboardShotProposal {
  schema_version: "1.0";
  proposal_sequence: number;
  visual_objective: string;
  desired_duration_us: number;
  word_start_index: number;
  word_end_index: number;
  clause_label: string;
  importance: number;
  camera: CameraPlan;
  action: ActionPlan;
  transition_in: TransitionPlan;
  transition_out: TransitionPlan;
  character_reference_ids: UUID[];
  location_reference_id: UUID | null;
  evidence_references: StoryboardSourceReference[];
  incoming_continuity: ContinuityState;
  expected_outgoing_continuity: ContinuityState;
  warnings: StructuredNote[];
}

export interface StoryboardShot {
  schema_version: "1.0";
  shot_id: UUID;
  storyboard_run_id: UUID;
  segment_id: UUID;
  global_sequence: number;
  segment_sequence: number;
  script_segment_id: UUID;
  narration_segment_id: UUID;
  start_us: number;
  end_us: number;
  global_start_us: number;
  global_end_us: number;
  usable_duration_us: number;
  requested_generation_duration_us: number;
  trim_start_us: number;
  trim_end_us: number;
  transition_handle_us: number;
  word_start_index: number;
  word_end_index: number;
  clause_label: string;
  visual_objective: string;
  camera: CameraPlan;
  action: ActionPlan;
  character_reference_ids: UUID[];
  location_reference_id: UUID | null;
  prop_references: string[];
  evidence_references: StoryboardSourceReference[];
  transition_in: TransitionPlan;
  transition_out: TransitionPlan;
  incoming_continuity: ContinuityState;
  expected_outgoing_continuity: ContinuityState;
  capability_profile_id: string;
  capability_hash: string;
  warnings: StructuredNote[];
  provenance: Record<string, unknown>;
}

export interface StoryboardSegment {
  schema_version: "1.0";
  segment_id: UUID;
  storyboard_run_id: UUID;
  script_segment_id: UUID;
  narration_segment_id: UUID;
  sequence: number;
  narration_duration_us: number;
  global_start_us: number;
  input_hash: string;
  shot_count: number;
  attempt_count: number;
  repair_attempt_count: number;
  warnings: StructuredNote[];
}

export interface TimingAdjustment {
  schema_version: "1.0";
  segment_sequence: number;
  proposal_sequence: number;
  shot_sequence: number;
  kind: TimingAdjustmentKind;
  proposed_duration_us: number;
  canonical_duration_us: number;
  delta_us: number;
  reason: string;
}

export interface TimingManifestEntry {
  schema_version: "1.0";
  shot_id: UUID;
  global_sequence: number;
  segment_sequence: number;
  script_segment_id: UUID;
  narration_segment_id: UUID;
  global_start_us: number;
  global_end_us: number;
  usable_duration_us: number;
  requested_generation_duration_us: number;
  trim_start_us: number;
  trim_end_us: number;
  transition_handle_us: number;
}

export interface TimingManifest {
  schema_version: "1.0";
  storyboard_run_id: UUID;
  project_id: UUID;
  script_id: UUID;
  script_version: number;
  narration_run_id: UUID;
  capability_profile_id: string;
  capability_hash: string;
  retimer_version: string;
  contract_version: string;
  segment_boundaries_us: number[];
  total_narration_duration_us: number;
  total_usable_duration_us: number;
  total_requested_generation_duration_us: number;
  total_transition_handle_us: number;
  residual_allocation_us: number;
  entries: TimingManifestEntry[];
  adjustments: TimingAdjustment[];
  warnings: StructuredNote[];
}

export interface StoryboardValidationDiagnostic {
  schema_version: "1.0";
  code: StoryboardDiagnosticCode;
  severity: "error" | "warning";
  repairable: boolean;
  message: string;
  entity_path: string;
  segment_sequence: number;
  shot_sequence: number;
  measured_us: number | null;
  expected_us: number | null;
}

export interface StoryboardValidationReport {
  schema_version: "1.0";
  valid: boolean;
  diagnostics: StoryboardValidationDiagnostic[];
  checked_segment_sequences: number[];
  covered_duration_us: number;
  expected_duration_us: number;
}

export interface StoryboardProviderRequest {
  schema_version: "1.0";
  idempotency_key: string;
  project_id: UUID;
  episode_model_id: UUID;
  episode_model_hash: string;
  script_id: UUID;
  script_version: number;
  script_segment_id: UUID;
  segment_sequence: number;
  narration_run_id: UUID;
  narration_segment_id: UUID;
  narration_asset_id: UUID;
  measured_duration_us: number;
  narration_text: string;
  word_timings: NarrationBoundary[];
  approved_boundaries: NarrationBoundary[];
  evidence_references: StoryboardSourceReference[];
  available_character_ids: UUID[];
  available_location_ids: UUID[];
  anonymous_speaker_label: string | null;
  incoming_continuity: ContinuityState;
  capability: VisualProviderCapability;
  contract_version: string;
  prompt_version: string;
  provider_options: Record<string, string | number | boolean>;
  validation_diagnostics: StoryboardValidationDiagnostic[];
  trace_context: Record<string, string>;
  attempt_number: number;
}

export interface StoryboardProviderResult {
  schema_version: "1.0";
  proposals: StoryboardShotProposal[];
  expected_incoming_continuity: ContinuityState;
  expected_outgoing_continuity: ContinuityState;
  provider: string;
  model: string;
  provider_request_id: string;
  idempotency_key: string;
  attempt_number: number;
  usage: Record<string, number>;
  redacted_response_metadata: Record<string, string | number | boolean>;
  warnings: StructuredNote[];
}

export interface Storyboard {
  schema_version: "1.0";
  storyboard_id: UUID;
  storyboard_run_id: UUID;
  project_id: UUID;
  version: number;
  episode_model_id: UUID;
  episode_model_hash: string;
  script_id: UUID;
  script_version: number;
  script_hash: string;
  narration_run_id: UUID;
  capability_profile_id: string;
  capability_hash: string;
  contract_version: string;
  director_version: string;
  prompt_version: string;
  retimer_version: string;
  input_hash: string;
  total_duration_us: number;
  segments: StoryboardSegment[];
  shots: StoryboardShot[];
  warnings: StructuredNote[];
  provenance: Record<string, unknown>;
}

export interface StoryboardResult {
  schema_version: "1.0";
  storyboard_run_id: UUID;
  project_id: UUID;
  status: "storyboard_complete" | "storyboard_failed";
  selected: boolean;
  storyboard_id: UUID | null;
  storyboard_asset_id: UUID | null;
  timing_manifest_asset_id: UUID | null;
  validation_report_asset_id: UUID | null;
  segment_count: number;
  shot_count: number;
  total_duration_us: number;
  repair_attempt_count: number;
  provider: string;
  model: string;
  estimated_cost: string;
  actual_cost: string;
  currency: string;
  error_code: string | null;
  warnings: StructuredNote[];
}

export interface VideoStreamInfo {
  codec: string;
  width: number;
  height: number;
  frame_rate: number;
  pixel_format: string | null;
}

export interface AudioStreamInfo {
  codec: string;
  sample_rate: number | null;
  channels: number | null;
}

export interface MediaProbeResult {
  schema_version: "1.0";
  duration_seconds: number;
  format_name: string;
  byte_size: number;
  video: VideoStreamInfo;
  audio_streams: AudioStreamInfo[];
  raw_probe: Record<string, unknown>;
}

export interface AudioExtractionResult {
  schema_version: "1.0";
  asset_id: UUID;
  sha256: string;
  duration_seconds: number;
  sample_rate: number;
  channels: number;
  codec: string;
}

export interface SceneBoundary {
  sequence: number;
  start_seconds: number;
  end_seconds: number;
  confidence: number;
}

export interface SceneDetectionResult {
  schema_version: "1.0";
  threshold: number;
  duration_seconds: number;
  scenes: SceneBoundary[];
}

export interface ExtractedFrame {
  schema_version: "1.0";
  asset_id: UUID;
  scene_sequence: number;
  timestamp_seconds: number;
  sha256: string;
  width: number;
  height: number;
}

export interface MediaProcessingResult {
  schema_version: "1.0";
  project_id: UUID;
  source_video_id: UUID;
  source_asset_id: UUID;
  probe: MediaProbeResult;
  audio: AudioExtractionResult;
  scene_detection: SceneDetectionResult;
  frames: ExtractedFrame[];
}

export interface TranscriptionWarning {
  code: string;
  message: string;
  chunk_sequence: number | null;
}

export interface TimeInterval {
  start_seconds: number;
  end_seconds: number;
}

export interface AudioChunk {
  schema_version: "1.0";
  asset_id: UUID;
  parent_audio_asset_id: UUID;
  sequence: number;
  start_seconds: number;
  end_seconds: number;
  overlap_before_seconds: number;
  overlap_after_seconds: number;
  byte_size: number;
  sha256: string;
  codec: string;
  sample_rate: number;
  idempotency_key: string;
}

export interface TranscriptWord {
  text: string;
  start_seconds: number;
  end_seconds: number;
  confidence: number | null;
}

export interface TranscriptSegment {
  schema_version: "1.0";
  sequence: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker_label: string | null;
  confidence: number | null;
  source_chunk_ids: UUID[];
  words: TranscriptWord[];
}

export interface SpeakerTurn {
  schema_version: "1.0";
  sequence: number;
  speaker_label: string;
  start_seconds: number;
  end_seconds: number;
  confidence: number | null;
  source_chunk_ids: UUID[];
  provider: string;
  model: string;
  alternate_labels: string[];
  warnings: TranscriptionWarning[];
}

export interface ChunkTranscriptionResult {
  schema_version: "1.0";
  chunk: AudioChunk;
  provider: string;
  model: string;
  provider_request_id: string;
  attempt: number;
  language: string | null;
  text: string;
  segments: TranscriptSegment[];
  words: TranscriptWord[];
  confidence: number | null;
  raw_metadata: Record<string, unknown>;
  warnings: TranscriptionWarning[];
}

export interface DiarizationResult {
  schema_version: "1.0";
  provider: string;
  model: string;
  provider_request_ids: string[];
  turns: SpeakerTurn[];
  warnings: TranscriptionWarning[];
}

export interface TranscriptCoverage {
  schema_version: "1.0";
  voiced_seconds: number;
  covered_voiced_seconds: number;
  ratio: number;
  passed: boolean;
  uncovered_intervals: TimeInterval[];
}

export interface TranscriptionResult {
  schema_version: "1.0";
  project_id: UUID;
  run_id: UUID;
  transcript_id: UUID;
  source_video_id: UUID;
  source_audio_asset_id: UUID;
  transcript_asset_id: UUID;
  status: "transcribed";
  language: string | null;
  text: string;
  segments: TranscriptSegment[];
  speaker_turns: SpeakerTurn[];
  coverage: TranscriptCoverage;
  warnings: TranscriptionWarning[];
}

export interface CanonicalTranscriptArtifact {
  schema_version: "1.0";
  project_id: UUID;
  run_id: UUID;
  transcript_id: UUID;
  source_video_id: UUID;
  source_audio_asset_id: UUID;
  language: string | null;
  text: string;
  segments: TranscriptSegment[];
  speaker_turns: SpeakerTurn[];
  coverage: TranscriptCoverage;
  warnings: TranscriptionWarning[];
}

export type SubtitleSourceType = "embedded" | "sidecar" | "provider";

export interface SubtitleCue {
  schema_version: "1.0";
  sequence: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker_hint: string | null;
}

export interface SubtitleCandidate {
  schema_version: "1.0";
  candidate_id: string;
  source_type: SubtitleSourceType;
  provider: string;
  provider_subtitle_id: string | null;
  provider_file_id: number | null;
  asset_id: UUID | null;
  stream_index: number | null;
  language: string | null;
  subtitle_format: string;
  hearing_impaired: boolean;
  forced: boolean;
  release_name: string | null;
  file_name: string | null;
  fps: number | null;
  download_count: number;
  metadata: Record<string, unknown>;
}

export interface SubtitleQuality {
  schema_version: "1.0";
  candidate_id: string;
  score: number;
  cue_count: number;
  timeline_coverage: number;
  voiced_coverage: number | null;
  sync_offset_seconds: number | null;
  sync_correlation: number | null;
  passed: boolean;
  reasons: string[];
}

export interface SubtitleSearchRequest {
  schema_version: "1.0";
  idempotency_key: string;
  movie_hash: string | null;
  byte_size: number | null;
  query: string | null;
  imdb_id: string | null;
  season_number: number | null;
  episode_number: number | null;
  languages: string[];
}

export interface ProviderSubtitleDownload {
  schema_version: "1.0";
  candidate_id: string;
  provider: string;
  provider_request_id: string;
  file_name: string;
  media_type: string;
  content: string;
  remaining_downloads: number | null;
}

export interface CanonicalSubtitleTranscriptArtifact {
  schema_version: "1.0";
  project_id: UUID;
  subtitle_run_id: UUID;
  transcript_id: UUID;
  source_video_id: UUID;
  source_subtitle_asset_id: UUID;
  language: string | null;
  text: string;
  segments: TranscriptSegment[];
  coverage: TranscriptCoverage;
  candidate: SubtitleCandidate;
  quality: SubtitleQuality;
  warnings: TranscriptionWarning[];
}

export interface SubtitleImportResult {
  schema_version: "1.0";
  project_id: UUID;
  subtitle_run_id: UUID;
  transcript_id: UUID;
  source_video_id: UUID;
  source_subtitle_asset_id: UUID;
  transcript_asset_id: UUID;
  status: "subtitle_imported";
  language: string | null;
  text: string;
  segments: TranscriptSegment[];
  coverage: TranscriptCoverage;
  candidate: SubtitleCandidate;
  quality: SubtitleQuality;
  warnings: TranscriptionWarning[];
}

export interface StructuredNote { code: string; message: string }
export type SourceReferenceType = "transcript_segment" | "speaker_turn" | "source_scene" | "frame" | "contact_sheet" | "project";
export interface SourceReference { schema_version: "1.0"; reference_type: SourceReferenceType; reference_id: UUID; scene_id: UUID | null; start_ms: number | null; end_ms: number | null }
export interface AnalysisObservation { schema_version: "1.0"; claim: string; source_references: SourceReference[] }
export interface AnalysisInference extends AnalysisObservation { confidence: number }
export interface AliasEvidence { alias: string; source_references: SourceReference[] }
export interface CharacterCandidate { schema_version: "1.0"; character_id: UUID; canonical_name: string; aliases: string[]; alias_evidence: AliasEvidence[]; anonymous: boolean; confidence: number; source_references: SourceReference[] }
export interface LocationCandidate { schema_version: "1.0"; location_id: UUID; canonical_name: string; aliases: string[]; alias_evidence: AliasEvidence[]; confidence: number; source_references: SourceReference[] }
export interface StateEvent { schema_version: "1.0"; state_event_id: UUID; entity_id: UUID; scene_id: UUID; sequence: number; description: string; confidence: number; source_references: SourceReference[] }
export interface Relationship { schema_version: "1.0"; relationship_id: UUID; source_character_id: UUID; target_character_id: UUID; description: string; confidence: number; source_references: SourceReference[] }
export interface PlotBeat { schema_version: "1.0"; plot_beat_id: UUID; sequence: number; scene_ids: UUID[]; character_ids: UUID[]; summary: string; importance: number; payoff_score: number; mandatory: boolean; source_references: SourceReference[] }
export interface BeatDependency { cause_beat_id: UUID; effect_beat_id: UUID; source_references: SourceReference[] }
export interface UnresolvedAmbiguity { schema_version: "1.0"; ambiguity_id: UUID; description: string; candidate_ids: UUID[]; source_references: SourceReference[] }
export interface CanonicalScene { scene_id: UUID; sequence: number; source_start_ms: number; source_end_ms: number; summary: string; dramatic_purpose: string; character_ids: UUID[]; location_id: UUID | null; confidence: number; source_references: SourceReference[] }
export interface SceneEvidenceExcerpt { text: string; speaker_label: string | null; source_reference: SourceReference }
export interface EpisodeAnalysis { schema_version: "1.0"; episode_id: UUID; project_id: UUID; source_video_id: UUID; evidence_package_id: UUID; title: string; duration_ms: number; logline: string; genre: string[]; tone: string[]; characters: CharacterCandidate[]; locations: LocationCandidate[]; scenes: CanonicalScene[]; state_events: StateEvent[]; relationships: Relationship[]; plot_beats: PlotBeat[]; beat_dependencies: BeatDependency[]; unresolved_ambiguities: UnresolvedAmbiguity[]; source_references: SourceReference[]; assumptions: StructuredNote[]; warnings: StructuredNote[] }
export interface AnalysisValidationError { code: string; entity_path: string; invalid_value: string | number | boolean | null; source_reference: SourceReference | null; explanation: string }
export interface AnalysisValidationReport { schema_version: "1.0"; valid: boolean; errors: AnalysisValidationError[]; warnings: StructuredNote[] }
export interface EpisodeAnalysisResult { schema_version: "1.0"; analysis_run_id: UUID; episode_analysis_id: UUID | null; analysis_asset_id: UUID | null; version: number | null; validation_report: AnalysisValidationReport }
export interface SceneAnalysisResult { schema_version:"1.0"; scene_id:UUID; sequence:number; source_start_ms:number; source_end_ms:number; summary:string; dramatic_purpose:string; observed_characters:CharacterCandidate[]; character_aliases:Record<string,string[]>; anonymous_speaker_references:string[]; location_candidates:LocationCandidate[]; state_changes:StateEvent[]; important_actions:AnalysisObservation[]; candidate_plot_beats:PlotBeat[]; candidate_causal_links:BeatDependency[]; visual_motifs:string[]; direct_observations:AnalysisObservation[]; inferences:AnalysisInference[]; confidence:number; source_references:SourceReference[]; assumptions:StructuredNote[]; warnings:StructuredNote[]; unresolved_questions:string[] }
export interface SceneAnalysisRequest { schema_version:"1.0"; project_id:UUID; evidence_package_id:UUID; scene_id:UUID; sequence:number; source_start_ms:number; source_end_ms:number; input_hash:string; idempotency_key:string; contract_version:string; prompt_version:string; provider_configuration_version:string; evidence_references:SourceReference[]; evidence_excerpts:SceneEvidenceExcerpt[]; provider_options:Record<string,string|number|boolean> }
export interface EpisodeSynthesisRequest { schema_version:"1.0"; project_id:UUID; evidence_package_id:UUID; source_video_id:UUID; duration_ms:number; input_hash:string; idempotency_key:string; contract_version:string; prompt_version:string; provider_configuration_version:string; scene_result_ids:UUID[]; scene_results:SceneAnalysisResult[]; validation_errors:AnalysisValidationError[] }
export interface ProviderMetadata { provider:string; model:string; provider_request_id:string; attempt_number:number; prompt_version:string; contract_version:string; input_hash:string; redacted_response_metadata:Record<string,string|number|boolean>; input_tokens:number|null; output_tokens:number|null; warnings:StructuredNote[]; validation_status:"pending"|"valid"|"invalid" }
export interface ProviderSceneAnalysisResult { output:SceneAnalysisResult; metadata:ProviderMetadata }
export interface ProviderEpisodeAnalysisResult { output:EpisodeAnalysis; metadata:ProviderMetadata }

// T23 observability and cost-ledger contracts (schema version 1.0).
export type ProviderFailureClass = "TRANSPORT" | "TIMEOUT" | "RATE_LIMIT" | "AUTHENTICATION" | "AUTHORIZATION" | "INVALID_REQUEST" | "PROVIDER_REJECTED" | "CONTENT_FILTER" | "CONTRACT_VALIDATION" | "QUALITY_FAILURE" | "BUDGET_EXCEEDED" | "CANCELLED" | "DATABASE" | "STORAGE" | "TEMPORAL" | "INTERNAL" | "UNKNOWN";
export type UsageUnit = "INPUT_TOKEN" | "OUTPUT_TOKEN" | "CACHED_INPUT_TOKEN" | "AUDIO_INPUT_SECOND" | "AUDIO_OUTPUT_SECOND" | "IMAGE_INPUT" | "IMAGE_OUTPUT" | "VIDEO_OUTPUT_SECOND" | "REQUEST" | "STORAGE_BYTE" | "COMPUTE_SECOND";
export interface UsageQuantity { readonly schema_version: "1.0"; readonly unit: UsageUnit; readonly quantity: string; readonly provider_reported: boolean; readonly estimation_method?: string | null; readonly source_field?: string | null; readonly warnings: readonly string[]; }
export interface FailureClassification { readonly schema_version: "1.0"; readonly failure_class: ProviderFailureClass; readonly error_code: string; readonly retryable: boolean; readonly sanitized_message: string; readonly diagnostic_metadata: Readonly<Record<string, unknown>>; }
export interface ProjectCostSummary { readonly schema_version: "1.0"; readonly project_id: string; readonly warning_cap: string; readonly hard_cap: string; readonly reserved_amount: string; readonly committed_amount: string; readonly released_amount: string; readonly remaining_amount: string; readonly by_provider: Readonly<Record<string, string>>; readonly by_model: Readonly<Record<string, string>>; readonly by_operation: Readonly<Record<string, string>>; readonly by_reason: Readonly<Record<string, string>>; }

// T11 compression and comedy script pipeline contracts (schema version 1.0).
export type RecapMode = "full_recap" | "highlight_reel";
export type StructuralRole = "setup" | "inciting_incident" | "escalation" | "climax" | "resolution" | "supporting";
export type JokeType = "commentary" | "analogy" | "exaggeration" | "contrast" | "callback" | "character_observation" | "visual_gag" | "wordplay";
export type SegmentType = "NARRATION" | "DIALOGUE" | "PAUSE";
export type SpeakerKind = "narrator" | "character" | "anonymous";
export type ApprovalRecommendation = "approve" | "revise" | "reject";
export type CoverageClassification = "covered" | "partial" | "missing";
export interface ChannelVoiceConfig { schema_version: "1.0"; narrator_persona: string; tone_keywords: string[]; catchphrases: string[] }
export interface PlotCompressionRequest { schema_version: "1.0"; project_id: UUID; episode_analysis_id: UUID; episode_analysis: EpisodeAnalysis; input_hash: string; idempotency_key: string; contract_version: string; prompt_version: string; provider_configuration_version: string; target_duration_ms: number; target_words: number; target_words_per_minute: number; required_beat_ids: UUID[]; excluded_topics: string[]; recap_mode: RecapMode; provider_options: Record<string, string | number | boolean> }
export interface CompressedPlotBeat { schema_version: "1.0"; plot_beat_id: UUID; sequence: number; summary: string; structural_role: StructuralRole; mandatory: boolean; payoff_score: number; character_ids: UUID[]; scene_ids: UUID[]; source_references: SourceReference[] }
export interface OmittedPlotBeat { schema_version: "1.0"; plot_beat_id: UUID; reason: string; may_cause_confusion: boolean; confusion_explanation: string | null }
export interface ConnectiveExplanation { cause_beat_id: UUID; effect_beat_id: UUID; explanation: string }
export interface BeatWordAllocation { plot_beat_id: UUID; words: number; estimated_duration_ms: number }
export interface WordBudget { total_target_words: number; allocations: BeatWordAllocation[] }
export interface PacingAllocation { plot_beat_id: UUID; estimated_duration_ms: number }
export interface CompressedPlotPlan { schema_version: "1.0"; plan_id: UUID; project_id: UUID; episode_analysis_id: UUID; logline: string; selected_beats: CompressedPlotBeat[]; omitted_beats: OmittedPlotBeat[]; connective_explanations: ConnectiveExplanation[]; pacing_plan: PacingAllocation[]; word_budget: WordBudget; source_refs: SourceReference[]; assumptions: StructuredNote[]; warnings: StructuredNote[] }
export interface ScriptProviderMetadata { schema_version: "1.0"; provider: string; model: string; provider_request_id: string; operation: "compress_plot" | "write_script" | "edit_script"; attempt_number: number; input_hash: string; prompt_version: string; contract_version: string; rubric_version: string | null; redacted_response_metadata: Record<string, string | number | boolean>; input_tokens: number | null; output_tokens: number | null; warnings: StructuredNote[]; validation_status: "pending" | "valid" | "invalid" }
export interface ProviderCompressedPlotResult { output: CompressedPlotPlan; metadata: ScriptProviderMetadata }
export interface TextSpan { start: number; end: number }
export interface JokeAnnotation { schema_version: "1.0"; joke_id: UUID; joke_type: JokeType; setup_span: TextSpan | null; punchline_span: TextSpan | null; callback_id: UUID | null; source_beat_ids: UUID[]; confidence: number | null; validation_status: "pending" | "valid" | "invalid" }
export interface ScriptSegment { schema_version: "1.0"; segment_id: UUID; sequence: number; type: SegmentType; speaker_kind: SpeakerKind; speaker_character_id: UUID | null; anonymous_speaker_label: string | null; text: string; plot_beat_ids: UUID[]; source_scene_ids: UUID[]; joke_annotations: JokeAnnotation[]; visual_gag: string | null; estimated_duration_ms: number; voice_direction: string; locked: boolean; content_hash: string }
export interface Callback { schema_version: "1.0"; callback_id: UUID; setup_segment_id: UUID; payoff_segment_id: UUID; description: string }
export interface BeatCoverage { schema_version: "1.0"; plot_beat_id: UUID; segment_ids: UUID[]; coverage: CoverageClassification; mandatory: boolean; diagnostics: StructuredNote[] }
export interface ComedyWritingRequest { schema_version: "1.0"; project_id: UUID; episode_analysis_id: UUID; compressed_plot_plan_id: UUID; input_hash: string; idempotency_key: string; contract_version: string; prompt_version: string; provider_configuration_version: string; compressed_plot: CompressedPlotPlan; channel_voice: ChannelVoiceConfig; humor_intensity: number; prohibited_patterns: string[]; target_words: number; recap_mode: RecapMode; locked_segments: ScriptSegment[]; revision_feedback: string | null; provider_options: Record<string, string | number | boolean> }
export interface RecapScript { schema_version: "1.0"; script_id: UUID; version: number; parent_script_id: UUID | null; project_id: UUID; episode_analysis_id: UUID; compressed_plot_plan_id: UUID; target_duration_ms: number; target_word_count: number; actual_word_count: number; voice_profile_ref: string; humor_intensity: number; cold_open_text: string | null; segments: ScriptSegment[]; callbacks: Callback[]; beat_coverage: BeatCoverage[]; source_refs: SourceReference[]; assumptions: StructuredNote[]; warnings: StructuredNote[] }
export interface ProviderRecapScriptResult { output: RecapScript; metadata: ScriptProviderMetadata }
export interface ScriptValidationError { code: string; entity_path: string; invalid_value: string | number | boolean | null; explanation: string }
export interface ScriptValidationReport { schema_version: "1.0"; valid: boolean; errors: ScriptValidationError[]; warnings: StructuredNote[] }
export interface ComedyRubric { schema_version: "1.0"; rubric_version: string; dimensions: string[]; approval_overall_min: number; approval_plot_fidelity_min: number }
export interface ComedyRubricScores { schema_version: "1.0"; plot_fidelity: number; clarity: number; joke_density: number; joke_variety: number; punchline_placement: number; spoken_rhythm: number; pacing: number; callback_quality: number; repetition: number; narratability: number; overall: number }
export interface ComedyIssue { segment_id: UUID | null; description: string; rubric_dimension: string; severity: "minor" | "major" | "blocking" }
export interface ScriptEdit { segment_id: UUID; old_text: string; new_text: string; reason: string; rubric_dimensions: string[]; plot_beat_ids: UUID[]; changes_word_count: boolean; was_locked: boolean }
export interface ScriptDiff { schema_version: "1.0"; from_version: number | null; to_version: number; added_segment_ids: UUID[]; removed_segment_ids: UUID[]; changed_segments: ScriptEdit[]; unchanged_segment_ids: UUID[] }
export interface ComedyEditRequest { schema_version: "1.0"; project_id: UUID; script_id: UUID; script_version: number; recap_script: RecapScript; compressed_plot: CompressedPlotPlan; rubric: ComedyRubric; prior_review_id: UUID | null; attempt_number: number; input_hash: string; idempotency_key: string; contract_version: string; prompt_version: string; rubric_version: string; provider_configuration_version: string }
export interface ComedyEditResult { schema_version: "1.0"; scores: ComedyRubricScores; issues: ComedyIssue[]; edits: ScriptEdit[]; revised_script: RecapScript; approval_recommendation: ApprovalRecommendation }
export interface ProviderComedyEditResult { output: ComedyEditResult; metadata: ScriptProviderMetadata }
export interface ScriptGenerationResult { schema_version: "1.0"; generation_run_id: UUID; compressed_plot_plan_id: UUID | null; script_id: UUID | null; script_version: number | null; status: string; validation_report: ScriptValidationReport | null; review_scores: ComedyRubricScores | null; revision_count: number }

export type VideoProvider = "runway" | "fake";
export type RunwayModel = "gen4_turbo" | "gen4.5";
export type VideoTaskStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";
export interface MotionIntent { schema_version: "1.0"; shot_id: UUID; shot_sequence: number; visual_purpose: string; primary_action: string; start_pose: string; expected_end_pose: string; camera_movement: string; motion_intensity: string; subject_priority: string[]; character_state: string[]; prop_state: string[]; environment_motion: string[]; timing_beats: string[]; continuity_invariants: string[]; negative_motion_constraints: string[] }
export interface MotionPromptPackage { schema_version: "1.0"; intent: MotionIntent; compiler_version: string; template_version: string; prompt: string; prompt_hash: string; diagnostics: string[]; provider_parameters: Record<string,unknown> }
export interface VideoProviderRequest { schema_version: "1.0"; application_idempotency_key: string; project_id: UUID; animation_run_id: UUID; animation_item_id: UUID; storyboard_id: UUID; storyboard_version: number; shot_id: UUID; shot_sequence: number; first_keyframe_asset_id: UUID; first_keyframe_sha256: string; last_keyframe_asset_id: UUID | null; last_keyframe_sha256: string | null; compiled_motion_prompt: string; provider: VideoProvider; model: RunwayModel; requested_duration_seconds: number; width: number; height: number; output_format: "mp4"; seed: number | null; provider_options: Record<string,unknown>; trace_context: Record<string,string>; attempt_number: number; provider_configuration_version: string }
export interface VideoProviderTask { schema_version: "1.0"; provider: VideoProvider; model: RunwayModel; remote_task_id: string; requested_at: string; status: VideoTaskStatus; provider_request_id: string | null; attempt_number: number; requested_duration_seconds: number; progress: number | null; usage: Record<string,number>; failure_reason: string | null; provider_error_code: string | null; metadata: Record<string,string|number|boolean>; completed_at: string | null; last_polled_at: string | null; latency_ms: number | null; application_idempotency_key: string; provider_configuration_version: string }
export interface VideoProviderResult extends VideoProviderTask { output_count: number }
export interface VideoProbeResult { schema_version: "1.0"; container: string; video_codec: string; audio_codec: string | null; width: number; height: number; display_aspect_ratio: string; pixel_format: string; frame_rate: string; timebase: string; duration_seconds: number; frame_count: number | null; byte_size: number; sha256: string; ffprobe_json: Record<string,unknown>; ffprobe_version: string }
export interface VideoValidationDiagnostic { schema_version: "1.0"; code: string; severity: "error"|"warning"; message: string }
export interface VideoValidationReport { schema_version: "1.0"; valid: boolean; probe: VideoProbeResult | null; diagnostics: VideoValidationDiagnostic[] }
export interface VideoTrimManifest { schema_version: "1.0"; trim_in_seconds: number; trim_out_seconds: number; usable_duration_seconds: number; ffmpeg_arguments: string[]; encoding_profile: string }
export interface GeneratedVideoCandidate { schema_version: "1.0"; generated_video_id: UUID; original_asset_id: UUID; canonical_asset_id: UUID; shot_id: UUID; selected: boolean; validation: VideoValidationReport }
export interface ShotAnimationResult { schema_version: "1.0"; shot_id: UUID; status: "completed"|"reused"|"polling"|"failed"; remote_task_id: string|null; candidate: GeneratedVideoCandidate|null; error_code: string|null }
export interface AnimationRunRequest { schema_version: "1.0"; project_id: UUID; storyboard_id: UUID|null; image_generation_run_id: UUID|null; idempotency_key: string; provider_configuration_version: string; provider: VideoProvider; model: RunwayModel|null; shot_id: UUID|null }
export interface AnimationRunResult { schema_version: "1.0"; run_id: UUID; storyboard_id: UUID; image_generation_run_id: UUID; requested_count: number; submitted_count: number; polling_count: number; completed_count: number; reused_count: number; failed_count: number; status: string }
export interface AnimationResult extends AnimationRunResult { items: ShotAnimationResult[] }

// T17 captions and deterministic rendering. Canonical time is integer microseconds.
export interface RenderInputReference { schema_version: "1.0"; asset_id: UUID; sha256: string; media_type: string; role: string }
export interface RenderTransition { schema_version: "1.0"; kind: "cut" | "crossfade"; duration_us: number; handle_in_us: number; handle_out_us: number }
export interface CaptionWord { schema_version: "1.0"; sequence: number; text: string; start_us: number; end_us: number }
export interface CaptionValidationDiagnostic { schema_version: "1.0"; code: string; severity: "warning" | "error"; message: string; cue_sequence: number | null }
export interface CaptionCue { schema_version: "1.0"; sequence: number; start_us: number; end_us: number; lines: string[]; word_start: number; word_end: number }
export interface CaptionTrack { schema_version: "1.0"; caption_track_id: UUID; language: string; cues: CaptionCue[]; duration_us: number; safe_zone_percent: number; pipeline_version: "captions/1" }
export interface RenderVideoProfile { schema_version: "1.0"; width: 1920; height: 1080; frame_rate: 24 | 30; codec: "libx264"; codec_profile: "high"; pixel_format: "yuv420p"; normalization_policy: "scale_crop" | "scale_pad" }
export interface RenderAudioProfile { schema_version: "1.0"; codec: "aac"; sample_rate_hz: 48000; channels: 2; bitrate_kbps: number; integrated_lufs: number; true_peak_dbtp: number; max_lra: number }
export interface RenderJobResult { schema_version: "1.0"; render_job_id: UUID; render_identity: string; status: string; manifest_asset_id: UUID | null; srt_asset_id: UUID | null; webvtt_asset_id: UUID | null; final_video_asset_id: UUID | null; verification_report_asset_id: UUID | null; reused: boolean }

// ---------------------------------------------------------------------------
// T18 review-UI control-plane projections.
// These mirror `vidgen.contracts.review`; the web app must not restate them.
// ---------------------------------------------------------------------------

export type ApiErrorCode =
  | "validation_failed"
  | "not_found"
  | "precondition_required"
  | "version_conflict"
  | "idempotency_key_required"
  | "idempotency_key_mismatch"
  | "workflow_not_started"
  | "upload_incomplete"
  | "render_not_verified"
  | "render_stale"
  | "shot_not_retryable"
  | "attempt_not_eligible"
  | "budget_denied"
  | "provider_unavailable"
  | "rate_limited"
  | "internal_error";

export interface ApiErrorField {
  schema_version: "1.0";
  field: string;
  code: string;
  message: string;
}

export interface ApiError {
  schema_version: "1.0";
  code: ApiErrorCode;
  summary: string;
  retryable: boolean;
  current_version: number | null;
  workflow_id: string | null;
  stage: string | null;
  fields: ApiErrorField[];
  correlation_id: string | null;
  /** The originating domain code where a route raises a narrower one. */
  detail_code: string | null;
}

export type PipelineStage =
  | "upload"
  | "media_processing"
  | "transcript_acquisition"
  | "evidence"
  | "episode_analysis"
  | "script_generation"
  | "narration"
  | "storyboard"
  | "keyframes"
  | "animation"
  | "shot_orchestration"
  | "captions"
  | "rendering"
  | "review";

export const PIPELINE_STAGE_ORDER: readonly PipelineStage[] = [
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
];

export type StageState =
  | "pending"
  | "running"
  | "complete"
  | "failed"
  | "skipped"
  | "cancelled";

export interface StageTimelineEntry {
  schema_version: "1.0";
  stage: PipelineStage;
  state: StageState;
  started_at: string | null;
  completed_at: string | null;
  detail_code: string | null;
}

export interface WorkflowStatusProjection {
  schema_version: "1.0";
  project_id: UUID;
  workflow_id: string | null;
  run_id: string | null;
  status: string;
  current_stage: PipelineStage | null;
  completed_stages: PipelineStage[];
  cancelled: boolean;
  started_at: string | null;
  updated_at: string | null;
  elapsed_seconds: number | null;
  total_shot_count: number;
  completed_shot_count: number;
  failed_shot_count: number;
  retryable_failure_count: number;
  render_status: string | null;
  stages: StageTimelineEntry[];
  progress_percentage: number | null;
}

export interface ProjectEventProjection {
  schema_version: "1.0";
  event_id: number;
  project_id: UUID;
  workflow_id: string | null;
  event_type: string;
  stage: PipelineStage | null;
  status: string;
  progress_percentage: number | null;
  completed_shot_count: number | null;
  total_shot_count: number | null;
  retryable_failure_count: number | null;
  render_status: string | null;
  cost_summary_version: number | null;
  warning_code: string | null;
  failure_code: string | null;
  created_at: string;
}

export interface TranscriptSegmentProjection {
  schema_version: "1.0";
  segment_id: UUID;
  sequence: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker_label: string | null;
  confidence: number | null;
  edited: boolean;
  row_version: number;
}

export interface TranscriptProjection {
  schema_version: "1.0";
  transcript_id: UUID;
  project_id: UUID;
  version: number;
  language: string | null;
  origin: "transcription" | "subtitle";
  duration_seconds: number;
  coverage_score: number;
  selected: boolean;
  row_version: number;
  source_asset_id: UUID | null;
  segments: TranscriptSegmentProjection[];
}

export interface ScriptSegmentProjection {
  schema_version: "1.0";
  segment_id: UUID;
  stable_segment_id: UUID;
  sequence: number;
  segment_type: string;
  speaker_kind: string;
  speaker_label: string | null;
  text: string;
  visual_gag: string | null;
  joke_annotation_count: number;
  plot_beat_ids: string[];
  word_count: number;
  estimated_duration_ms: number;
  measured_narration_duration_ms: number | null;
  locked: boolean;
  content_hash: string;
  row_version: number;
}

export interface ScriptSummaryProjection {
  schema_version: "1.0";
  script_id: UUID;
  version: number;
  status: string;
  selected: boolean;
  actual_word_count: number;
  target_word_count: number;
  target_duration_ms: number;
  parent_script_id: UUID | null;
  created_at: string;
  row_version: number;
}

export interface ScriptProjection {
  schema_version: "1.0";
  project_id: UUID;
  script: ScriptSummaryProjection;
  approved: boolean;
  segments: ScriptSegmentProjection[];
}

export interface InvalidationEntry {
  schema_version: "1.0";
  resource_type: string;
  resource_id: UUID;
  label: string;
  reason: string;
}

export interface InvalidationSet {
  schema_version: "1.0";
  entries: InvalidationEntry[];
  requires_confirmation: boolean;
}

export interface StoryboardShotProjection {
  schema_version: "1.0";
  shot_id: UUID;
  stable_shot_id: UUID;
  global_sequence: number;
  segment_sequence: number;
  script_segment_id: UUID;
  global_start_us: number;
  global_end_us: number;
  usable_duration_us: number;
  requested_generation_duration_us: number;
  trim_start_us: number;
  trim_end_us: number;
  visual_objective: string;
  camera_framing: string | null;
  camera_movement: string | null;
  character_references: string[];
  location_reference: string | null;
  transition_in: string | null;
  transition_out: string | null;
  workflow_status: string;
  selected_keyframe_asset_id: UUID | null;
  selected_video_asset_id: UUID | null;
  provider: string | null;
  model: string | null;
  attempt_count: number;
  cost_amount: string | null;
  warning_code: string | null;
  failure_code: string | null;
  row_version: number;
}

export interface StoryboardProjection {
  schema_version: "1.0";
  project_id: UUID;
  storyboard_run_id: UUID;
  version: number;
  selected: boolean;
  shot_count: number;
  segment_count: number;
  total_duration_us: number;
  timing_manifest_asset_id: UUID | null;
  row_version: number;
  shots: StoryboardShotProjection[];
}

export interface ShotAttemptProjection {
  schema_version: "1.0";
  attempt_id: UUID;
  kind: "keyframe" | "video";
  attempt_number: number;
  status: string;
  asset_id: UUID | null;
  provider: string;
  model: string;
  provider_task_id: string | null;
  generation_identity: string | null;
  prompt_version: string | null;
  generated_duration_us: number | null;
  usable_duration_us: number | null;
  cost_amount: string | null;
  failure_class: string | null;
  selected: boolean;
  created_at: string;
}

export interface ShotDetailProjection {
  schema_version: "1.0";
  shot: StoryboardShotProjection;
  child_workflow_id: string | null;
  child_workflow_status: string;
  child_workflow_retryable: boolean;
  identity_hash: string | null;
  trim_instructions_asset_id: UUID | null;
  source_evidence_ids: string[];
  keyframe_attempts: ShotAttemptProjection[];
  video_attempts: ShotAttemptProjection[];
  regeneration_history: string[];
}

export interface ShotStatusProjection {
  schema_version: "1.0";
  shot_id: UUID;
  child_workflow_id: string | null;
  status: string;
  retryable: boolean;
  attempt_count: number;
  failure_code: string | null;
  row_version: number;
}

export interface ShotRegenerationResult {
  schema_version: "1.0";
  shot_id: UUID;
  child_workflow_id: string;
  new_identity_hash: string;
  previous_identity_hash: string | null;
  preserved_attempt_ids: UUID[];
  invalidation: InvalidationSet;
  row_version: number;
}

export interface RenderApprovalProjection {
  schema_version: "1.0";
  approval_id: UUID;
  render_job_id: UUID;
  approved_by: string;
  approved_at: string;
  lineage_hash: string;
  applies_to_current_lineage: boolean;
}

export interface RenderFailure {
  schema_version: "1.0";
  classification: "lineage" | "validation" | "transient" | "cancelled" | "execution";
  code: string;
  message: string;
  retryable: boolean;
  diagnostics: CaptionValidationDiagnostic[];
}

export interface RenderInputSelection {
  schema_version: "1.0";
  project_id: UUID;
  render_job_id: UUID;
  approved_script_id: UUID;
  approved_script_version: number;
  approved_script_hash: string;
  narration_run_id: UUID;
  narration_asset_id: UUID;
  narration_duration_us: number;
  narration_word_timing_hash: string;
  storyboard_run_id: UUID;
  storyboard_hash: string;
  timing_manifest_id: UUID;
  timing_manifest_hash: string;
  shot_count: number;
  references: RenderInputReference[];
  visual_qa_result_ids: UUID[];
  repair_result_ids: UUID[];
  character_reference_ids: UUID[];
  location_reference_ids: UUID[];
  audio_asset_ids: UUID[];
  subtitle_mode: "selectable" | "burn_in" | "both";
  render_profile: string;
  target_duration_us: number;
  aspect_ratio: string;
  output_width: number;
  output_height: number;
  frame_rate: number;
  caption_configuration_hash: string;
  visual_qa_policy_version: string;
  pipeline_version: string;
  input_hash: string;
  resolved_at: string;
}

export interface RenderExecutionRequest {
  schema_version: "1.0";
  render_job_id: UUID;
  worker_id: string;
  lease_seconds: number;
  max_attempts: number;
  heartbeat_seconds: number;
  execution_timeout_seconds: number;
  minimum_free_bytes: number;
  trace_context: Record<string, string>;
}

export interface RenderExecutionCheckpoint {
  schema_version: "1.0";
  render_job_id: UUID;
  status: RenderExecutionStatus;
  attempt: number;
  progress_percent: number;
  phase: string;
  input_hash: string | null;
  manifest_asset_id: UUID | null;
  caption_asset_id: UUID | null;
  final_video_asset_id: UUID | null;
  updated_at: string;
}

/** The durable T17b render-job states. */
export type RenderExecutionStatus =
  | "render_queued"
  | "render_claiming"
  | "render_preparing"
  | "render_manifest_ready"
  | "render_rendering"
  | "render_verifying"
  | "render_persisting"
  | "render_complete"
  | "render_failed"
  | "render_cancelled";

export interface RenderExecutionProgress {
  schema_version: "1.0";
  render_job_id: UUID;
  project_id: UUID;
  status: RenderExecutionStatus;
  progress_percent: number;
  phase: string | null;
  attempt: number;
  claimed_by: string | null;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  cancel_requested: boolean;
  failure_code: string | null;
  failure_classification: string | null;
}

export interface RenderExecutionResult {
  schema_version: "1.0";
  render_job_id: UUID;
  project_id: UUID;
  status: RenderExecutionStatus;
  reused: boolean;
  render_identity: string | null;
  input_hash: string | null;
  output_sha256: string | null;
  manifest_asset_id: UUID | null;
  caption_srt_asset_id: UUID | null;
  caption_webvtt_asset_id: UUID | null;
  final_video_asset_id: UUID | null;
  verification_report_asset_id: UUID | null;
  measured_duration_us: number | null;
  expected_duration_us: number | null;
  renderer_version: string | null;
  ffmpeg_version: string | null;
  attempt: number;
  warning_codes: string[];
  failure: RenderFailure | null;
  completed_at: string | null;
}

export interface RenderWorkerResult {
  schema_version: "1.0";
  render_job_id: UUID;
  status: RenderExecutionStatus;
  reused: boolean;
  exit_code: number;
  final_video_asset_id: UUID | null;
  output_sha256: string | null;
  measured_duration_us: number | null;
  failure_code: string | null;
  failure_classification: string | null;
}

export interface RenderActivityInput {
  schema_version: "1.0";
  project_id: UUID;
  render_job_id: UUID | null;
  idempotency_key: string;
  trace_context: Record<string, string>;
}

export interface RenderActivityResult {
  schema_version: "1.0";
  project_id: UUID;
  render_job_id: UUID;
  status: string;
  reused: boolean;
  progress_percent: number;
  render_identity: string | null;
  input_hash: string | null;
  output_sha256: string | null;
  final_render_asset_id: UUID | null;
  render_manifest_asset_id: UUID | null;
  measured_duration_us: number | null;
  attempt: number;
  error_code: string | null;
  failure_classification: string | null;
}

export interface RenderProjection {
  schema_version: "1.0";
  render_job_id: UUID;
  project_id: UUID;
  status: string;
  attempt: number;
  render_version: string;
  render_identity: string | null;
  selected: boolean;
  stale: boolean;
  verified: boolean;
  verification_summary: string | null;
  expected_duration_us: number | null;
  measured_duration_us: number | null;
  selected_shot_count: number;
  caption_language: string | null;
  caption_cue_count: number | null;
  subtitle_mode: string;
  integrated_loudness_lufs: number | null;
  true_peak_dbtp: number | null;
  warning_codes: string[];
  final_video_asset_id: UUID | null;
  srt_asset_id: UUID | null;
  webvtt_asset_id: UUID | null;
  verification_report_asset_id: UUID | null;
  manifest_asset_id: UUID | null;
  script_id: UUID | null;
  script_version: number | null;
  storyboard_run_id: UUID | null;
  narration_run_id: UUID | null;
  ffmpeg_version: string | null;
  lineage_hash: string | null;
  /** T17b execution state: real progress, and the real reason a render failed. */
  progress_percent: number;
  checkpoint: string | null;
  attempt_count: number;
  cancel_requested: boolean;
  failure_code: string | null;
  failure_classification: string | null;
  output_sha256: string | null;
  input_hash: string | null;
  renderer_version: string | null;
  /** True only for a complete, verified, non-stale render with a stored asset. */
  downloadable: boolean;
  approval: RenderApprovalProjection | null;
  row_version: number;
  completed_at: string | null;
}

export interface ProjectSummaryProjection {
  schema_version: "1.0";
  project_id: UUID;
  name: string;
  status: string;
  current_stage: PipelineStage | null;
  progress_percentage: number | null;
  target_duration_seconds: number;
  visual_style: string;
  humor_intensity: number;
  updated_at: string;
  committed_cost_amount: string | null;
  hard_cap_amount: string | null;
  has_failures: boolean;
  row_version: number;
}

/** The T23 cost summary as projected by `GET /projects/{id}/costs` (camelCase). */
export interface ProjectCostSummaryResponse {
  projectId: UUID;
  warningCap: string;
  hardCap: string;
  reservedAmount: string;
  committedAmount: string;
  releasedAmount: string;
  remainingAmount: string;
  warningPercentage: string | null;
  hardPercentage: string | null;
  byProvider: Record<string, string>;
  byModel: Record<string, string>;
  byOperation: Record<string, string>;
  byReason: Record<string, string>;
}

export interface ProviderAttemptListItem {
  id: UUID;
  provider: string;
  model: string;
  operation: string;
  status: string;
  failureClass: string | null;
  latencyMs: number | null;
  startedAt: string;
}

export interface ProviderAttemptListResponse {
  total: number;
  offset: number;
  limit: number;
  items: ProviderAttemptListItem[];
}

export interface PipelineFailureListItem {
  id: UUID;
  workflowId: string | null;
  stage: string;
  failureClass: string;
  errorCode: string;
  retryable: boolean;
  status: string;
}

export interface PipelineFailureListResponse {
  items: PipelineFailureListItem[];
}

export type ReferenceWorkflowStatus =
  | "references_queued"
  | "references_selecting"
  | "references_building"
  | "references_generating"
  | "references_validating"
  | "references_awaiting_approval"
  | "references_binding"
  | "references_complete"
  | "references_failed"
  | "references_cancelled";

export interface ReferenceWorkflowInput {
  schema_version: "1.0";
  project_id: UUID;
  episode_analysis_id: UUID;
  storyboard_run_id: UUID;
  reference_run_id: UUID;
  idempotency_key: string;
  trace_context: Record<string, string>;
}

export interface ReferenceApprovalSignal {
  schema_version: "1.0";
  project_id: UUID;
  reference_run_id: UUID;
  approval_id: UUID;
  idempotency_key: string;
}

export interface ReferenceWorkflowResult {
  schema_version: "1.0";
  project_id: UUID;
  reference_run_id: UUID;
  status: ReferenceWorkflowStatus;
  approved_version_ids: UUID[];
  affected_shot_ids: UUID[];
  cancelled: boolean;
}

// --- T20 semantic visual QA --------------------------------------------------
// These mirror the strict Pydantic contracts in `vidgen.contracts.visual_qa` and
// the bounded API projections in `apps/api/schemas/visual_qa.py`. No provider
// payloads, signed URLs, or media bytes ever cross this boundary.

export type VisualQATargetType = "keyframe" | "video";
export type VisualQAOutcome = "PASS" | "FAIL" | "REVIEW";
export type VisualQAShotImportance = "utility" | "normal" | "hero";
export type VisualQARoutingRecommendation =
  | "NONE"
  | "TARGETED_REPAIR"
  | "PROMPT_SIMPLIFICATION"
  | "NEW_SEED"
  | "COMPOSITION_SPLIT"
  | "HUMAN_REVIEW";

export type VisualQADimensionName =
  | "character_identity"
  | "character_count"
  | "location"
  | "wardrobe_and_state"
  | "action_and_motion"
  | "composition"
  | "anatomy_and_artifacts"
  | "continuity_and_style";

export type VisualQARepairCode =
  | "WRONG_CHARACTER_IDENTITY"
  | "MISSING_PRIMARY_CHARACTER"
  | "EXTRA_CHARACTER"
  | "WRONG_CHARACTER_COUNT"
  | "WRONG_WARDROBE"
  | "WRONG_CHARACTER_STATE"
  | "WRONG_LOCATION"
  | "WRONG_LOCATION_STATE"
  | "MISSING_REQUIRED_PROP"
  | "WRONG_PROP_OWNERSHIP"
  | "MISSING_MANDATORY_ACTION"
  | "WRONG_ACTION"
  | "INSUFFICIENT_MOTION"
  | "EXCESSIVE_MOTION"
  | "CAMERA_PLAN_MISMATCH"
  | "COMPOSITION_MISMATCH"
  | "SCREEN_DIRECTION_CONTRADICTION"
  | "FACE_BREAKAGE"
  | "ANATOMY_BREAKAGE"
  | "UNINTENDED_TEXT"
  | "STYLE_DRIFT"
  | "CONTINUITY_BREAK"
  | "BLACK_VIDEO"
  | "EXCESSIVE_FREEZE"
  | "EXCESSIVE_FLICKER"
  | "DURATION_MISMATCH"
  | "DECODE_FAILURE"
  | "PROMPT_TOO_COMPLEX"
  | "TOO_MANY_CHARACTERS"
  | "TOO_MANY_REFERENCES"
  | "AMBIGUOUS_VISUAL_EVIDENCE"
  | "HUMAN_REVIEW_REQUIRED";

export interface VisualQADimensionProjection {
  dimension: VisualQADimensionName | string;
  applicable: boolean;
  raw_score: number;
  weight: number;
  effective_weight: number;
  weighted_contribution: number;
  confidence: number;
  warning_codes: string[];
  hard_failure_codes: string[];
  repair_codes: string[];
  finding_summaries: string[];
}

export interface VisualQADiagnosticProjection {
  code: string;
  outcome: "pass" | "warning" | "hard_failure" | "not_applicable" | string;
  diagnostic_code: string;
  measurement: number | null;
  threshold: number | null;
  evidence_timestamp_us: number | null;
  repair_code: string | null;
  message: string;
}

export interface VisualQASampleProjection {
  sample_id: UUID;
  sequence: number;
  sample_type: string;
  requested_timestamp_us: number;
  actual_timestamp_us: number;
  shot_relative_timestamp_us: number;
  frame_asset_id: UUID | null;
  frame_sha256: string;
  selection_reason: string;
  contact_sheet_position: number | null;
}

export interface VisualQABoundingBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface VisualQAEvidenceProjection {
  evidence_id: UUID;
  finding_id: UUID;
  evidence_type: string;
  sample_id: UUID | null;
  frame_asset_id: UUID | null;
  shot_relative_timestamp_us: number | null;
  source_relative_timestamp_us: number | null;
  contact_sheet_position: number | null;
  bounding_box: VisualQABoundingBox | null;
  compared_reference_asset_id: UUID | null;
  confidence: number;
  explanation: string;
}

export interface VisualQARunProjection {
  qa_run_id: UUID;
  project_id: UUID;
  shot_id: UUID;
  target_type: VisualQATargetType | string;
  status: string;
  outcome: VisualQAOutcome | null;
  score: number | null;
  pass_threshold: number | null;
  importance: VisualQAShotImportance | string;
  hard_failure: boolean;
  repair_recommendation: VisualQARoutingRecommendation | string | null;
  repair_codes: string[];
  warning_codes: string[];
  confidence: number | null;
  adjudicated: boolean;
  human_review_decision: "approved" | "rejected" | null;
  provider: string;
  model: string;
  cost_microusd: number;
  rubric_version: string;
  threshold_version: string;
  sampling_version: string;
  sample_count: number;
  deterministic_warning_count: number;
  row_version: number;
  created_at: string;
  completed_at: string | null;
}

export interface VisualQAAdjudicationProjection {
  policy_version: string;
  triggered_by: string[];
  first_pass_provider: string;
  first_pass_model: string;
  adjudicator_provider: string;
  adjudicator_model: string;
  adjudicator_confidence: number;
  decided: boolean;
  disagreement_summary: string[];
  resulting_outcome_hint: VisualQAOutcome;
  attempts_used: number;
}

export interface VisualQARunDetailProjection extends VisualQARunProjection {
  dimensions: VisualQADimensionProjection[];
  diagnostics: VisualQADiagnosticProjection[];
  samples: VisualQASampleProjection[];
  compared_reference_asset_ids: UUID[];
  contact_sheet_asset_id: UUID | null;
  report_asset_id: UUID | null;
  adjudication: VisualQAAdjudicationProjection | null;
}

export interface VisualQACollectionResponse {
  project_id: UUID;
  items: VisualQARunProjection[];
}

export interface VisualQAEvidenceResponse {
  qa_run_id: UUID;
  items: VisualQAEvidenceProjection[];
  samples: VisualQASampleProjection[];
}

export interface VisualQARunRequest {
  provider: "fake" | "openai";
  targets: VisualQATargetType[];
}

export interface VisualQARunResponse {
  status: "queued";
  project_id: UUID;
  shot_id: UUID | null;
  targets: string[];
  resource_id: UUID;
  row_version: number;
}

export interface VisualQADecisionRequest {
  reason: string;
}

export interface VisualQADecisionResponse {
  qa_run_id: UUID;
  review_id: UUID;
  decision: "approved" | "rejected";
  resulting_gate: string;
  row_version: number;
}

/* --- T21 repair and fallback routing ------------------------------------- */

export type RepairFailureCategory =
  | "prompt_issue"
  | "reference_issue"
  | "seed_issue"
  | "provider_issue"
  | "impossible_shot";

export type RepairSeverity = "targeted" | "structural" | "unrecoverable";

export type RepairAttemptKind =
  | "original"
  | "same_provider_repair"
  | "alternate_provider"
  | "deterministic_fallback";

export type RepairAttemptStatus =
  | "planned"
  | "submitted"
  | "polling"
  | "downloading"
  | "revalidating"
  | "passed"
  | "failed"
  | "cancelled";

export type RepairRunState =
  | "REPAIR_PLANNING"
  | "REPAIRING"
  | "ALTERNATE_PROVIDER"
  | "FALLBACK_RENDERING"
  | "REVALIDATING"
  | "HUMAN_REVIEW_REQUIRED"
  | "LOCKED"
  | "REPAIR_FAILED";

export type RepairRoute =
  | "same_provider_repair"
  | "alternate_provider"
  | "deterministic_fallback"
  | "resume_provider_operation"
  | "upstream_reference_correction"
  | "human_review_required"
  | "select_passing_attempt";

export type HumanReviewReason =
  | "repair_budget_exhausted"
  | "project_budget_denied"
  | "attempt_limit_reached"
  | "fallback_ineligible"
  | "impossible_shot"
  | "upstream_reference_correction"
  | "deterministic_failure"
  | "cancelled_before_paid_attempt";

export type RepairAction =
  | "retry"
  | "cancel"
  | "acknowledge"
  | "resolve"
  | "restart_after_reference_correction";

/**
 * A safe structured view of what one repair changed in the prompt. The prompt
 * itself never crosses the API boundary; only the classified delta does.
 */
export interface RepairPromptDeltaProjection {
  planner_version: string;
  repair_reason: string;
  added_clauses: string[];
  removed_clauses: string[];
  rewritten_clauses: string[][];
  preserved_constraint_ids: string[];
  touched_constraint_ids: string[];
  before_prompt_hash: string;
  after_prompt_hash: string;
  seed_changed: boolean;
  previous_seed: number | null;
  new_seed: number | null;
}

export interface RepairAttemptProjection {
  attempt_id: UUID;
  attempt_ordinal: number;
  attempt_kind: RepairAttemptKind;
  status: RepairAttemptStatus;
  predecessor_attempt_id: UUID | null;
  root_animation_attempt_id: UUID;
  provider: string;
  model: string;
  provider_operation_id: string | null;
  capability_profile_hash: string | null;
  prompt_hash: string | null;
  prompt_delta: RepairPromptDeltaProjection | null;
  seed: number | null;
  output_asset_ids: UUID[];
  output_qa_result_id: UUID | null;
  qa_score: number | null;
  qa_outcome: string | null;
  estimated_cost: string;
  actual_cost: string;
  currency: string;
  failure_category: RepairFailureCategory | null;
  failure_code: string | null;
  selected: boolean;
  created_at: string;
  completed_at: string | null;
}

export interface RepairDecisionProjection {
  decision_id: UUID;
  sequence: number;
  route: RepairRoute;
  rationale: string[];
  failure_category: RepairFailureCategory | null;
  repair_codes: string[];
  human_review_reason: HumanReviewReason | null;
  estimated_next_cost: string;
  budget_remaining: string | null;
  planner_version: string;
  policy_version: string;
  created_at: string;
}

export interface RepairFallbackProjection {
  repair_attempt_id: UUID;
  renderer_version: string;
  render_identity: string;
  input_asset_ids: UUID[];
  exact_duration_us: number;
  width: number;
  height: number;
  frame_rate: string;
  pixel_format: string;
  video_codec: string;
  output_asset_id: UUID;
  manifest_asset_id: UUID;
  qa_result_id: UUID | null;
}

export interface RepairBudgetProjection {
  currency: string;
  total_repair_cost: string;
  estimated_repair_cost: string;
  per_shot_repair_cost_limit: string | null;
  project_hard_cap: string | null;
  project_remaining: string | null;
}

export interface RepairRunProjection {
  repair_run_id: UUID;
  project_id: UUID;
  shot_id: UUID;
  state: RepairRunState;
  root_animation_attempt_id: UUID;
  triggering_qa_result_id: UUID;
  failure_category: RepairFailureCategory | null;
  failure_severity: RepairSeverity | null;
  repair_code: string | null;
  qa_score: number | null;
  pass_threshold: number | null;
  hard_failure: boolean;
  hard_failure_reason: string | null;
  total_attempt_count: number;
  same_provider_repairs_used: number;
  alternate_provider_attempts_used: number;
  fallback_renders_used: number;
  selected_attempt_id: UUID | null;
  selected_asset_id: UUID | null;
  final_qa_result_id: UUID | null;
  final_qa_score: number | null;
  human_review_reason: HumanReviewReason | null;
  human_review_resolved: boolean;
  policy_version: string;
  planner_version: string;
  row_version: number;
  created_at: string;
  updated_at: string;
}

export interface RepairRunDetailProjection extends RepairRunProjection {
  attempts: RepairAttemptProjection[];
  decisions: RepairDecisionProjection[];
  fallback: RepairFallbackProjection | null;
  budget: RepairBudgetProjection;
}

export interface RepairCollectionResponse {
  project_id: UUID;
  items: RepairRunProjection[];
}

export interface RepairActionRequest {
  action: RepairAction;
  reason: string;
}

export interface RepairActionResponse {
  repair_run_id: UUID;
  action: RepairAction;
  accepted: boolean;
  state: RepairRunState;
  code: string;
  row_version: number;
}

// --- T22 final editorial QA -------------------------------------------------

export type FinalQADecision = "PASS" | "FAIL" | "REVIEW";

export type FinalQAStatus =
  | "FINAL_QA_QUEUED"
  | "FINAL_QA_VALIDATING_INPUTS"
  | "FINAL_QA_CHECKING_MEDIA"
  | "FINAL_QA_CHECKING_CAPTIONS"
  | "FINAL_QA_ANALYZING"
  | "FINAL_QA_ADJUDICATING"
  | "FINAL_QA_REVIEW_REQUIRED"
  | "FINAL_QA_PASSED"
  | "FINAL_QA_FAILED";

export type FinalQAPhase =
  | "INPUT_VALIDATION"
  | "DETERMINISTIC_MEDIA_QA"
  | "CAPTION_QA"
  | "EDITORIAL_ANALYSIS"
  | "ADJUDICATION"
  | "COMPLETION_GATE";

export type FinalFindingSeverity =
  | "blocking"
  | "review_required"
  | "warning"
  | "informational";

export type FinalRemediationTarget =
  | "NONE"
  | "RERENDER_T17"
  | "REBUILD_CAPTIONS_T17"
  | "REMIX_AUDIO_T17"
  | "REGENERATE_SHOT_T16"
  | "REPAIR_SHOT_T21"
  | "CORRECT_REFERENCE_T19"
  | "CORRECT_SCRIPT_UPSTREAM"
  | "HUMAN_EDITORIAL_REVIEW";

export type FinalReviewDecision = "accept" | "reject" | "escalate";

export interface FinalMeasurementProjection {
  container_format: string;
  byte_size: number;
  video_codec: string;
  audio_codec: string;
  width: number | null;
  height: number | null;
  pixel_format: string;
  frame_rate: string;
  container_duration_us: number | null;
  video_duration_us: number | null;
  audio_duration_us: number | null;
  sample_rate_hz: number | null;
  channels: number | null;
  integrated_lufs: number | null;
  true_peak_dbtp: number | null;
  clipping_ratio: number | null;
  video_decoded: boolean;
  audio_decoded: boolean;
  black_interval_count: number;
  freeze_interval_count: number;
  silence_interval_count: number;
  ffmpeg_version: string;
  ffprobe_version: string;
}

export interface FinalCheckProjection {
  check_id: UUID;
  check_type: string;
  code: string;
  status: "pass" | "fail" | "warning" | "not_applicable" | string;
  blocking: boolean;
  measurement: number | null;
  threshold: number | null;
  unit: string;
  start_us: number | null;
  end_us: number | null;
  cue_sequence: number | null;
  tool: string;
  tool_version: string;
  message: string;
}

export interface FinalDimensionProjection {
  category: string;
  applicable: boolean;
  score: number;
  confidence: number;
  blocking_finding_count: number;
  review_finding_count: number;
  warning_finding_count: number;
  summary: string;
}

export interface FinalEvidenceProjection {
  evidence_id: UUID;
  evidence_type: string;
  start_us: number;
  end_us: number;
  frame_asset_id: UUID | null;
  sample_id: UUID | null;
  contact_sheet_asset_id: UUID | null;
  contact_sheet_position: number | null;
  caption_cue_sequence: number | null;
  shot_id: UUID | null;
  measurement: number | null;
  threshold: number | null;
  explanation: string;
}

export interface FinalFindingProjection {
  finding_id: UUID;
  category: string;
  severity: FinalFindingSeverity | string;
  blocking: boolean;
  confidence: number;
  issue_code: string;
  summary: string;
  start_us: number;
  end_us: number;
  shot_ids: UUID[];
  caption_cue_sequences: number[];
  narration_segment_ids: UUID[];
  evidence: FinalEvidenceProjection[];
  expected_behavior: string;
  observed_behavior: string;
  remediation_target: FinalRemediationTarget | string;
  provenance: string;
  resolved_by_review: boolean;
}

export interface FinalRemediationProjection {
  target: FinalRemediationTarget | string;
  finding_ids: UUID[];
  shot_ids: UUID[];
  caption_cue_sequences: number[];
  reason: string;
  requires_new_render: boolean;
}

export interface FinalEditorialRunProjection {
  final_editorial_run_id: UUID;
  project_id: UUID;
  final_render_asset_id: UUID;
  render_manifest_asset_id: UUID;
  render_identity: string;
  final_qa_identity: string;
  input_hash: string;
  configuration_hash: string;
  report_version: string;
  status: FinalQAStatus | string;
  phase: FinalQAPhase | string;
  decision: FinalQADecision | null;
  selected: boolean;
  blocking_finding_count: number;
  review_finding_count: number;
  warning_finding_count: number;
  deterministic_failure_count: number;
  remediation_targets: string[];
  provider: string;
  model: string;
  adjudicated: boolean;
  cost_microusd: number;
  report_asset_id: UUID | null;
  contact_sheet_asset_id: UUID | null;
  error_code: string | null;
  row_version: number;
  created_at: string;
  completed_at: string | null;
}

export interface FinalEditorialRunDetailProjection extends FinalEditorialRunProjection {
  measurements: FinalMeasurementProjection | null;
  media_checks: FinalCheckProjection[];
  audio_checks: FinalCheckProjection[];
  caption_checks: FinalCheckProjection[];
  dimensions: FinalDimensionProjection[];
  findings: FinalFindingProjection[];
  remediation_routes: FinalRemediationProjection[];
  adjudication_confidence: number | null;
  adjudication_decided: boolean;
  gate_reasons: string[];
  timeline_duration_us: number;
}

export interface FinalEditorialCollectionResponse {
  project_id: UUID;
  items: FinalEditorialRunProjection[];
}

export interface FinalEditorialRunRequest {
  provider: "fake" | "openai";
  adjudicate: boolean;
}

export interface FinalEditorialCancelRequest {
  reason: string;
}

export interface FinalEditorialRunResponse {
  status: "queued" | "cancelled";
  project_id: UUID;
  final_render_asset_id: UUID | null;
  provider: string;
  resource_id: UUID;
  row_version: number;
}

export interface FinalEditorialReviewRequest {
  finding_id: UUID;
  decision: FinalReviewDecision;
  reason_code: string;
  reason: string;
}

export interface FinalEditorialReviewResponse {
  final_editorial_run_id: UUID;
  review_id: UUID;
  finding_id: UUID;
  decision: FinalReviewDecision;
  resulting_gate: FinalQADecision;
  row_version: number;
}

export interface FinalEditorialRemediationRequest {
  target: FinalRemediationTarget | string;
  finding_ids: UUID[];
}

export interface FinalEditorialRemediationResponse {
  final_editorial_run_id: UUID;
  target: string;
  routed_finding_ids: UUID[];
  requires_new_render: boolean;
  resource_id: UUID;
  row_version: number;
}

export interface FinalCompletionGateProjection {
  project_id: UUID;
  final_editorial_run_id: UUID | null;
  final_render_asset_id: UUID | null;
  decision: FinalQADecision | null;
  allowed: boolean;
  reason: string;
  blocking_finding_count: number;
  review_finding_count: number;
  deterministic_failure_count: number;
  gate_version: string;
  row_version: number;
}
