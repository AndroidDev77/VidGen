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
