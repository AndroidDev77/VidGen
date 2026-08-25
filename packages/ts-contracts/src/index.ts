export type UUID = string;

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
