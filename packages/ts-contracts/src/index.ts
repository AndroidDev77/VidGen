export type UUID = string;

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

export interface ShotDefinition {
  schema_version: "1.0";
  shot_id: UUID;
  segment_id: UUID;
  sequence: number;
  duration_seconds: number;
  location_id: UUID;
  character_states: CharacterState[];
  action: string;
  composition: string;
  camera_motion: string;
  visual_gag: string | null;
  image_prompt: string;
  video_prompt: string;
  negative_prompt: string;
  reference_asset_ids: UUID[];
  seed: number | null;
  max_provider_clip_seconds: number;
}

export interface Storyboard {
  schema_version: "1.0";
  project_id: UUID;
  script_revision: number;
  total_duration_seconds: number;
  visual_style: string;
  shots: ShotDefinition[];
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
