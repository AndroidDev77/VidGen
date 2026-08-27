export type UUID = string;
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
