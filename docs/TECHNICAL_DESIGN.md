# Animated Comedy Recap System

**Technical design and implementation backlog**  
**Version:** 1.0  
**Verified:** August 25, 2026  
**Target:** A restartable production pipeline that converts a 30 to 60+ minute source into a 4 to 6 minute, 1080p animated comedy recap.

## Implementation status

This document began as the complete target design and is maintained alongside the shipped code.
The current `main` branch and validated contract schemas remain authoritative when an original
example conflicts with an implemented interface.

| Scope | Status | Notes |
| --- | --- | --- |
| T01-T07 | Complete | Monorepo, contracts, database, asset storage, ingestion API, deterministic media processing, and restartable transcription are merged. |
| T07B | Complete extension | Embedded, sidecar, and OpenSubtitles acquisition now precedes T07 audio transcription fallback and writes the same canonical transcript model. |
| T08-T09 | Complete | Temporal project orchestration and deterministic scene evidence packages consume the selected canonical transcript regardless of subtitle or audio origin. |
| T10-T26 | Planned | Do not begin a later task until its dependencies and the current implementation are reconciled. |

When a roadmap task is completed, update this table, `README.md`, and any affected design section in
the same pull request.

## Fixed architectural decisions

| Area | Recommended choice | Why | Alternative |
| --- | --- | --- | --- |
| Core backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic | Python has the strongest media, ML, and provider SDK ecosystem. Pydantic supplies the same strict contracts to API routes, agents, and workers. | ASP.NET Core can own the public API if the team requires C#, while Python still owns media workers. Node.js is acceptable for the web gateway, but should not own FFmpeg and computer-vision work. |
| Workflow engine | Temporal Cloud with the Python SDK | Durable execution, activity retries, timers, child workflows per shot, signals for human approval, and workflow replay directly fit a long-running media pipeline. Temporal retry policies are native and explicit. [Temporal retry documentation](https://docs.temporal.io/encyclopedia/retry-policies) | Azure Durable Functions for an Azure-only organization. Use n8n only for notifications and upload integrations, never as the authoritative state machine. Celery and BullMQ are task queues, not full durable workflow histories. |
| API and creative models | OpenAI Responses API, `gpt-5.6-terra` for creative and editorial agents, `gpt-5.6-luna` for inexpensive extraction and first-pass QA | Both models support image input and Structured Outputs. Terra is the balanced default, while Luna is suitable for high-volume, schema-constrained work. Current model positioning and prices are in the [official OpenAI model catalog](https://developers.openai.com/api/docs/models). | Use `gpt-5.6-sol` only for failed editorial passes or exceptionally ambiguous source material. |
| Structured agent output | JSON Schema through OpenAI Structured Outputs, then Pydantic validation | Schema adherence is enforced at generation time, followed by application validation and database constraints. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) | Tool calling is useful when an agent must invoke a deterministic service, but ordinary stage outputs should use a structured response format. |
| Transcription | Subtitle-first acquisition, then `whisper-1` for timestamp-preserving audio transcription plus `gpt-4o-transcribe-diarize` for speaker segments | Embedded, sidecar, and OpenSubtitles text is preferred when adequate. Audio fallback uses `whisper-1` because the shipped T07 contract requires word or segment timestamps; diarization remains a separate pass. Files over 25 MB are split at silence boundaries. [OpenAI file transcription and diarization](https://developers.openai.com/api/docs/guides/speech-to-text) | Local Whisper large-v3 plus pyannote or WhisperX when media cannot leave the deployment boundary. |
| Image generation | OpenAI `gpt-image-2` through the Image API | Strong instruction following, image inputs, edits, and high-fidelity reference handling suit character sheets and shot keyframes. [OpenAI image generation](https://developers.openai.com/api/docs/guides/image-generation) | Vertex AI Imagen or a self-hosted diffusion workflow with character LoRAs for very high volume. |
| Video generation | Runway API adapter, default `gen4_turbo`, upgrade hero shots to `gen4.5`; Veo 3.1 Fast adapter as fallback | Runway exposes an official Python SDK, asynchronous tasks, image-to-video, and multiple models. Current rates are $0.05 per generated second for `gen4_turbo` and $0.12 for `gen4.5`. [Runway SDK](https://docs.dev.runwayml.com/api-details/sdks/), [Runway pricing](https://docs.dev.runwayml.com/guides/pricing/) | Direct Vertex AI Veo 3.1 Fast. It supports 4, 6, or 8 second outputs, first/last frames, and reference assets. [Veo 3.1 specifications](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/veo/3-1-generate) |
| Voice | OpenAI `gpt-4o-mini-tts`, `marin` or `cedar`, with explicit delivery instructions | Low integration complexity, controllable tone and speed, and good default narration quality. [OpenAI text to speech](https://developers.openai.com/api/docs/guides/text-to-speech) | ElevenLabs Multilingual v2 for the premium voice tier and Flash v2.5 for low latency. [ElevenLabs TTS](https://elevenlabs.io/docs/overview/capabilities/text-to-speech) |
| Vision QA | `gpt-5.6-luna` first pass, `gpt-5.6-terra` adjudication, plus deterministic OpenCV checks | Cheap frame-by-frame checks handle most shots. A stronger model only sees borderline or contradictory results. | A dedicated internal VLM can replace Luna after enough labeled QA data exists. |
| Durable data | PostgreSQL 16, Azure Blob Storage, Redis 7 | PostgreSQL holds canonical entities and workflow projections. Blob Storage holds immutable media. Redis is only a cache, rate limiter, and ephemeral semaphore store. | S3 and ElastiCache on AWS. Do not store workflow truth only in Redis. |
| Rendering | FFmpeg 7 in isolated render workers | Deterministic, scriptable, reproducible, and supports subtitles, loudness, mixing, scaling, and hardware encoding. | Remotion for template-heavy motion graphics, still calling FFmpeg for final encoding. |

## Options considered

### Backend and workflow

| Option | Decision | Reason |
| --- | --- | --- |
| Python | **Use** | Best fit for FFmpeg control, computer vision, model SDKs, Pydantic contracts, and Temporal workers. |
| Node.js | UI and optional edge/BFF only | Strong TypeScript ergonomics, but weaker as the main media-processing runtime. |
| ASP.NET Core | Valid enterprise API alternative | Excellent control plane and Azure integration, but it would still require Python workers, creating two backend ecosystems before they are needed. |
| Temporal | **Use** | Durable multi-hour workflows, child workflows per shot, explicit retries, timers, and human-review signals. |
| Azure Durable Functions | Azure-only alternative | Good managed orchestration, but more Azure coupling and less natural provider-task and child-workflow modeling for this pipeline. |
| AWS Step Functions | Do not use on the recommended Azure deployment | Capable state machine, but adds cross-cloud operational complexity and state-transition pricing. |
| Celery | Do not use as orchestrator | Good distributed task execution, but durable state, human waits, compensation, and workflow history would need custom implementation. |
| BullMQ | Do not use as orchestrator | Good Node queue, but the same durability and workflow-history gaps as Celery. |
| n8n | Integration helper only | Useful for notifications and publishing hooks, not for authoritative shot-level workflow state. |

### Video providers

| Provider | Decision | Strengths | Constraint and use |
| --- | --- | --- | --- |
| Runway | **Primary adapter** | Official typed SDK, asynchronous task model, clear per-second pricing, first-frame animation, and a multi-model API surface. | Default to cost-efficient Gen-4 Turbo and upgrade selected hero shots to Gen-4.5. |
| Veo 3.1 Fast | **Production fallback** | 1080p, first/last frames, references, extensions, and consistent 4/6/8 second contracts. | Direct Vertex access uses fixed quota for the documented GA model, so provision capacity before relying on it. |
| Kling 3.0 Omni | **Secondary adapter after MVP** | Multimodal references, element consistency, native audio options, storyboard control, and clips up to 15 seconds. [Official Kling 3.0 guide](https://app.klingai.com/global/quickstart/klingai-video-3-omni-model-user-guide) | Valuable for difficult character shots, but first run account-specific API, concurrency, callback, and cost contract tests. Do not couple canonical shot contracts to Kling-only fields. |
| OpenAI Sora 2 | Do not select as the primary video backend | Simple API and known per-second pricing. | The official model catalog currently marks Sora 2 as Legacy, so it should not anchor a new production system. [Sora 2 model page](https://developers.openai.com/api/docs/models/sora-2) |

Kling should implement the same `VideoGenerator` protocol. Its element IDs and motion-control inputs belong in `providerOverrides.kling`, while the canonical ShotDefinition remains provider-neutral.

# 1. Executive Architecture

The system is an asset-oriented workflow, not a single request. One parent Temporal workflow owns a project. It starts stage activities and one child workflow per shot. Every stage writes an immutable output artifact and a database projection. An input hash, contract version, prompt version, model version, and provider request ID make every result reproducible and restartable.

Creative agents may propose plot, jokes, and visual direction. They cannot advance states, retry themselves, select unbounded tools, or overwrite approved assets. Temporal decides what runs next. Deterministic workers perform file probing, splitting, timing, frame extraction, hashing, validation, mixing, and rendering.

```text
 [React Review UI]             [Optional YouTube]
          |                           ^
          v                           |
 [FastAPI Control Plane] <----> [PostgreSQL]
          |                           |
          v                           v
 [Temporal Parent Workflow] ---> [Asset/State Index]
          |
          +--> Ingest/FFmpeg workers ------+
          +--> Transcript/vision workers --+--> [Azure Blob Storage]
          +--> Reasoning agents -----------+
          +--> Voice/image providers ------+
          +--> Shot child workflows -------+
          |        | Generate -> QA -> Repair
          |        +------------------------+
          +--> Render worker ---------------> final.mp4

 External providers sit behind versioned adapter interfaces.
 Redis supplies cache, quotas, and short-lived concurrency permits only.
```

The canonical hierarchy is:

```text
Project
  SourceVideo
  EpisodeAnalysis
    Characters -> CharacterStates -> CharacterReferences
    Locations  -> LocationStates  -> LocationReferences
    Scenes -> PlotBeats
  RecapScript -> ScriptSegments -> NarrationAudio
  Storyboard -> Shots -> GenerationAttempts -> QAResults
  RenderJob -> FinalVideo -> Thumbnail and YouTubeMetadata
```

## Non-negotiable invariants

1. Blob objects are content-addressed by SHA-256 and never overwritten.
2. Database rows are versioned. Approval changes a pointer to the selected version.
3. Every activity is idempotent for `(operation, input_hash, contract_version, provider_config_version)`.
4. A shot child workflow owns only one `shot_id`. Retrying it cannot invalidate other shots.
5. Agent responses are rejected unless they pass JSON Schema, Pydantic, foreign-key, timing, and semantic validation.
6. The render manifest is the only input to FFmpeg. A render never queries "latest" assets while running.
7. Provider callbacks and polling updates are deduplicated by provider task ID and event ID.

# 2. End-to-End Workflow

## Stages 1 through 9: canonical episode construction

| # | Stage | Input | Processing | Output | Technology | Failure conditions | Retry strategy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Video ingestion | Upload session, source file, project settings | Stream directly to Blob Storage; compute SHA-256; run malware scan; call `ffprobe`; validate duration, codecs, streams, frame rate, rotation, and color metadata; create normalized proxy only when required | `SourceVideo`, immutable original asset, probe JSON, 720p analysis proxy | FastAPI signed upload, Azure Blob, FFprobe | Incomplete upload, duplicate chunks, unsupported/corrupt container, missing audio/video | Resume block upload; three network retries; deterministic validation is not retried; duplicate hash reuses existing asset after user confirmation |
| 2 | Audio extraction | Source asset URI and probe JSON | Extract mono 16 kHz FLAC for speech and stereo 48 kHz WAV stems for editorial use; preserve time origin; calculate duration and silence map | Speech FLAC chunks, program audio WAV, silence intervals | FFmpeg, `silencedetect` | Decode failure, duration drift over 100 ms, empty audio | Retry once with software decoder; normalize timestamps with `-fflags +genpts`; otherwise mark `NEEDS_INPUT` |
| 3 | Transcription | Speech FLAC chunks, project language hints | Split below 25 MB at silence boundaries with 1.5 s overlap; transcribe; reconcile overlaps by token alignment; store word or segment timing; run name normalization without changing timing | Canonical transcript, transcript segments, confidence and chunk provenance | OpenAI `gpt-transcribe`; optional local Whisper | Provider timeout, invalid timestamps, missing interval, overlap duplication | Exponential retries 2 s, 8 s, 30 s; retry the chunk only; fallback to local Whisper; fail if uncovered audio exceeds 2% |
| 4 | Speaker identification | Speech chunks, canonical transcript, optional known voice references | Run diarizing transcription; merge speaker spans; associate anonymous speaker labels with visual face tracks and dialogue context; preserve confidence and alternates | Speaker turns and speaker-to-character candidates | `gpt-4o-transcribe-diarize`, OpenCV face tracks, Episode Analyst | Speaker overlap, speaker fragmentation, low identity confidence, label count explosion | Re-diarize only affected window with wider context; merge embeddings; unresolved labels remain `speaker_unknown_N` and enter review |
| 5 | Scene detection | 720p proxy and timebase | Detect cuts, fades, and high-content changes; merge sub-second shots unless an action cut; separate camera shots from narrative scenes; keep exact source timecodes | Shot boundaries, preliminary scene groups | PySceneDetect AdaptiveDetector and ThresholdDetector, FFmpeg | Too few or too many cuts, variable-frame-rate drift, cuts inside flashes | Re-run with threshold profile chosen from cut density; verify against packet timestamps; manual boundary edit remains possible |
| 6 | Representative frames | Detected source shots | Extract midpoint, first clear face, last frame, and highest-quality frame; reject blur, black frames, and subtitles where possible; make contact sheets | Frame assets and frame scores linked to source timecodes | FFmpeg, OpenCV Laplacian blur, OCR, face detector | No usable frame, decode gap, burned text covers frame | Expand search window; use adjacent shot; retain low-quality frame with a flag instead of blocking analysis |
| 7 | Plot and character analysis | Transcript, speaker map, scenes, contact sheets, project references | Analyze scenes in chunks, then synthesize globally; identify named characters, goals, relationships, locations, causality, reveals, running motifs, and continuity states | `EpisodeAnalysis` draft | Episode Analyst, `gpt-5.6-terra`, Structured Outputs | Contract invalid, unsupported claim, character aliases duplicated, chronology gaps | Schema retry once with validation errors; second pass using only problematic scenes; final adjudication by stronger reasoning configuration |
| 8 | Structured episode representation | Analysis draft and deterministic media index | Resolve aliases; create stable IDs; attach every claim to source scene IDs and timecodes; compute embeddings for search; persist versioned canonical records | Validated Episode, Characters, Locations, Scenes, PlotBeats, state events | Pydantic, PostgreSQL, pgvector optional | Broken references, overlapping scene times, missing beat evidence, duplicate IDs | No blind retry; return violations to Episode Analyst, then validate again; keep prior valid version |
| 9 | Plot compression | Canonical episode, target duration, humor and spoiler settings | Allocate time budget to setup, escalation, climax, resolution, and connective jokes; select must-keep beats using causality and payoff; exclude redundant subplots | `CompressedPlotPlan` with 12 to 20 recap beats and word budgets | Plot Compressor, `gpt-5.6-terra` | Estimated word count cannot fit, missing causal bridge, climax omitted, unsupported beat | One critique-revision cycle; deterministic timing validator adjusts word budgets; human escalation if mandatory beats exceed target |

## Stages 10 through 18: script, references, and animation inputs

| # | Stage | Input | Processing | Output | Technology | Failure conditions | Retry strategy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | Comedy script generation | Compressed plot plan, character facts, humor intensity, target words, channel voice | Write original narration, optional rewritten dialogue, call-backs, visual gags, and transitions; tag every line with beat IDs and joke types | `RecapScript` v1 and `ScriptSegments` | Comedy Writer, `gpt-5.6-terra` | Plot contradiction, near-verbatim copying, weak joke density, word budget outside 5% | Regenerate only failing segments with surrounding context; never silently change approved segments |
| 11 | Script critique and revision | Script v1, compressed plot, rubric | Score plot fidelity, clarity, joke variety, punchline placement, repetition, pacing, and narratability; produce line-level edits; revalidate word count | Approved script candidate v2+ and editorial QA | Comedy Editor, `gpt-5.6-terra`; Sol escalation | Score below 85, beat coverage below 100% for mandatory beats, more than two consecutive exposition-only segments | Two targeted revisions; third failure enters script review UI |
| 12 | Narration generation | Approved script segments and voice profile | Generate narration per segment with consistent instructions; concatenate a preview; measure exact audio durations with `ffprobe`; forced-align text; store voice settings and hashes | Narration assets, word timings, measured segment durations | OpenAI `gpt-4o-mini-tts`, FFprobe, Whisper alignment | Mispronunciation, clipped audio, abnormal silence, duration outside speaking-rate envelope | Retry segment with pronunciation and pace instruction; optional ElevenLabs fallback; maximum three attempts |
| 13 | Storyboard generation | Approved script plus measured narration durations, episode model, provider capabilities | Convert each narration segment into one or more visual shots; solve shot durations against actual audio; select camera, action, transition, references, and expected continuity state | `Storyboard` and timing manifest | Storyboard Director plus deterministic retimer | Coverage gap, impossible duration, provider duration unsupported, excessive character count | Automatically split at clause or beat boundary; generate next supported duration and trim; targeted storyboard revision only |
| 14 | Character references | Character definitions, clean source frames, visual style spec | Build canonical character bible; generate turnaround, full body, face close-up, expression grid, palette, proportions, wardrobe variants, and accessories; embed reference identity | Character reference assets and versioned `CharacterDefinition` | `gpt-image-2` edit/generation; identity embeddings | Face or skin-tone drift, wardrobe ambiguity, source frame contaminated, inconsistent sheet panels | Generate panels separately if grid fails; visual QA; two prompt repairs; human chooses canonical sheet |
| 15 | Location references | Location definitions, representative frames, style spec | Generate wide establishing view, reverse angle, color and material palette, landmark list, day/night variants, and object layout | Location reference sheets and state variants | `gpt-image-2`, OpenCV palette extraction | Layout contradictions, missing landmarks, accidental text, wrong time of day | Repair prompt with invariant list; regenerate failed angle only; human lock for recurring locations |
| 16 | Shot prompt generation | Shot definition, character/location references, continuity snapshot, provider capabilities | Compile a provider-neutral visual intent, then provider-specific image and motion prompts; include positive constraints and structured negative constraints | Versioned prompt package with referenced asset IDs | Visual Prompt Engineer and prompt compiler | Missing reference, conflicting state, unsupported camera command, prompt too long | Deterministic prompt lint first; agent repairs conflicts; truncate only optional style adjectives |
| 17 | Keyframe/image generation | Prompt package, reference assets, aspect ratio | Generate first frame and optional last frame; score identity and composition before animation; optionally create foreground/background layers for 2.5D fallback | Candidate `GeneratedImage` attempts and selected keyframe | `gpt-image-2` Image API | Identity mismatch, wrong count, anatomy artifact, wrong location, text, aspect error | QA-repair loop, two retries; try reference edit instead of fresh generation; then human/fallback renderer |
| 18 | Image-to-video animation | Selected keyframes, motion prompt, exact coverage duration, provider policy | Pick provider/model through capability registry; request supported duration; poll task; verify codec/duration; trim excess or split long coverage; store provider task provenance | Candidate `GeneratedVideo` attempts | Runway SDK; Veo adapter fallback | Provider rejection, timeout, duration mismatch, motion drift, frozen frame, malformed output | Network retries do not create a new generation when task ID exists; two creative retries, one alternate-provider attempt; then 2.5D fallback or review |

## Stages 19 through 27: sound, QA, render, and delivery

| # | Stage | Input | Processing | Output | Technology | Failure conditions | Retry strategy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 19 | Sound effects and music | Storyboard audio cues, final timing, music profile | Retrieve or generate SFX; select loopable licensed music stems; place cues; sidechain music under narration; prohibit generated video audio from becoming authoritative unless explicitly selected | SFX, music assets, and audio mix manifest | Internal SFX catalog, Runway SFX or selected audio provider, FFmpeg | Missing cue, clipping, noisy generation, music gap | Retry cue twice; fall back to library asset or omit nonessential cue |
| 20 | Automatic visual QA | Generated video, sampled frames, optical-flow summary, ShotDefinition, references, adjacent approved shots | Evaluate identity, count, location, wardrobe/state, action, camera, anatomy, text, motion, and continuity; combine VLM scores with deterministic detectors | `QAResult` with score, hard failures, evidence frames, repair hints | OpenAI vision models, OpenCV, OCR, face embeddings | Model disagreement, unreadable frames, score below threshold, hard-fail rule | Luna first; Terra adjudicates scores 75 to 89 or contradictions; re-sample once on decode failure |
| 21 | Failed-shot regeneration | Failed QA result, previous prompt, generation metadata | Classify failure as prompt, reference, seed, provider, or impossible-shot issue; create minimal repaired prompt; preserve useful constraints; choose seed policy | New prompt and generation attempt linked to its predecessor | Prompt Repair Agent, shot child workflow | Same failure repeats, retry cap, provider unavailable | Two same-provider repairs, one alternate provider, then deterministic 2.5D fallback or `HUMAN_REVIEW_REQUIRED` |
| 22 | Video assembly | Locked shot assets and edit-decision list | Normalize clips to 1920x1080, 24 or 30 fps, yuv420p; trim precisely; apply transitions only where specified; concatenate against master timebase | Silent picture master and render manifest | FFmpeg filter graph | Missing selected asset, timestamp discontinuity, resolution/fps mismatch | Rebuild affected intermediate; render job remains idempotent; no provider regeneration |
| 23 | Captions | Script, forced-aligned narration word timings, style profile | Generate WebVTT and SRT; group words into readable 1 to 2 line cues; keep caption safe zones; optionally make ASS for styled burn-in | `.vtt`, `.srt`, `.ass` assets | Python caption builder, FFmpeg/libass | Timing overlap, line too long, text mismatch, cue outside duration | Re-align failed segment; deterministic cue reflow; human text edit if transcript mismatch remains |
| 24 | Final render | Picture master, narration, dialogue, SFX, music, captions | Mix stems, duck music, normalize to target loudness, encode H.264/AAC 1080p, attach selectable subtitles, optionally burn captions, write metadata, verify output | Final MP4, audio master, render report | FFmpeg and FFprobe | Clipping, A/V drift over 80 ms, black frames, invalid stream, loudness outside target | Re-render from immutable manifest; change only deterministic filter parameters; three attempts |
| 25 | Thumbnail, title, description | Script summary, selected hero frames, channel profile | Generate 3 thumbnail concepts, concise title candidates, description, chapters, keywords, and alt text; render title text deterministically | Thumbnail candidates and `PublicationMetadata` | `gpt-image-2` for artwork, Pillow/Sharp for text, `gpt-5.6-luna` for copy | Illegible text, wrong character, title too long, metadata contradicts recap | OCR and contrast checks; regenerate art without text; deterministic text overlay; human chooses candidate |
| 26 | Human review | Final preview, script, storyboard, shot QA, metadata | Reviewer can approve, edit script, lock assets, regenerate a shot, replace media, or request a new render; Temporal waits on a signal | Approval event, immutable review notes, selected versions | React UI, FastAPI, Temporal Signal/Update | Review timeout, edit invalidates downstream assets, concurrent edit conflict | Optimistic concurrency; compute dependency invalidation; resume only necessary child workflows |
| 27 | Optional YouTube upload | Approved MP4, thumbnail, captions, publication metadata, OAuth grant | Create resumable upload; set visibility and metadata; upload caption track and thumbnail; persist platform video ID and status | Publication record and upload receipts | YouTube Data API v3 | OAuth expiry, quota error, interrupted upload, processing rejection | Refresh token through secure provider; resume upload URI; back off quota errors; never duplicate after a returned video ID |

## Narration-driven timing algorithm

Let each narration segment have measured duration `N_i`. Add planned head and tail handles `H_i`, normally 80 to 250 ms, then set visual coverage `C_i = N_i + H_i`. The storyboard proposes visual sub-beats. A deterministic retimer converts them into provider jobs:

1. Split at sentence, clause, reaction, or gag boundaries, never at an arbitrary frame unless no semantic boundary exists.
2. Give each shot a desired edit duration between 2.2 and 7.5 seconds. Reaction inserts may be 1.0 to 2.2 seconds using still-image motion.
3. For a provider supporting continuous lengths up to `M`, create `ceil(C_i / M)` child shots and distribute the duration in proportion to sub-beat importance.
4. For a discrete provider such as Veo, round each requested generation up to the next supported value in `{4, 6, 8}` and trim the output to the edit duration.
5. If one visual must persist longer than the provider maximum, use a first/last-frame continuation where available, generate an extension, or cut to a reaction, insert, close-up, or deterministic pan on a second keyframe.
6. Never time-stretch generated video by more than 5%. Use `setpts` only for tiny corrections. Large gaps require another shot or an intentional freeze with camera movement.

The retimer must satisfy `abs(sum(edit_duration_ms) - final_audio_duration_ms) <= 40 ms` before rendering.

# 3. Agent Design

## Shared agent protocol

Every agent receives an immutable envelope. It has no database credentials and no authority to advance workflow state.

```json
{
  "schema_version": "1.0",
  "prompt_version": "episode-analyst/1.3.0",
  "project_id": "0191f1d0-54e0-7c74-9d0b-90bc8c8af101",
  "trace_id": "01J64Y7F2BDH0FZQMBP0A5W9NE",
  "input_asset_ids": [
    "0191f1d0-54e0-7c74-9d0b-90bc8c8af102",
    "0191f1d0-54e0-7c74-9d0b-90bc8c8af103"
  ],
  "constraints": {"target_duration_seconds": 300, "humor_intensity": 7},
  "payload": {}
}
```

All public outputs use the repository's snake-case Pydantic contracts, including an explicit `schema_version`. Provenance fields defined by the target contract must cite scene IDs, transcript span IDs, asset IDs, or approved reference IDs. Free-form factual claims without provenance are invalid.

## Episode Analyst

- **Responsibility:** Convert transcript, speakers, scene boundaries, and contact sheets into characters, locations, scene summaries, state changes, plot beats, causality, and unresolved ambiguities.
- **Input schema:** `EpisodeEvidence {sourceVideoId, transcriptSegments[], speakerTurns[], sourceScenes[], frameRefs[], optionalUserReferences[]}`.
- **Output schema:** `EpisodeAnalysis` from section 5.
- **System prompt responsibilities:** Preserve chronology; distinguish observation from inference; merge aliases only with evidence; attach every beat and character appearance to source references; record uncertainty instead of inventing facts.
- **Validation rules:** Scene times are monotonic and within source duration; every `characterId`, `locationId`, and `sceneId` resolves; every mandatory plot beat has at least one evidence reference; confidence is in `[0,1]`; no two canonical characters share the same stable ID.

## Plot Compressor

- **Responsibility:** Select the smallest causally complete plot that fits the target and produce per-beat word and time budgets.
- **Input schema:** `PlotCompressionRequest {episodeAnalysis, targetDurationMs, targetWords, requiredBeatIds[], excludedTopics[], recapMode}`.
- **Output schema:** `CompressedPlotPlan {logline, selectedBeats[], omittedBeats[], connectiveExplanations[], pacingPlan, wordBudget}`.
- **System prompt responsibilities:** Retain setup, inciting incident, escalation, climax, and resolution; preserve causes before effects; prefer beats with payoff; flag omitted subplots that make later events confusing.
- **Validation rules:** All mandatory beats selected; selected beat order respects dependency graph; word budgets sum to target within 2%; every omission has a reason; no new events.

## Comedy Writer

- **Responsibility:** Turn the compressed plan into original, performable narration and optional short character dialogue.
- **Input schema:** `ComedyWritingRequest {compressedPlot, characterFacts, channelVoice, humorIntensity, prohibitedPatterns[], targetWords}`.
- **Output schema:** `RecapScript` from section 5.
- **System prompt responsibilities:** Preserve plot facts; add commentary, analogy, exaggeration, callbacks, and visual gags; vary joke mechanisms; write for spoken rhythm; tag each joke and source beat.
- **Validation rules:** Target words within 5%; every selected beat covered; no more than 18 seconds of exposition without a joke or visual gag at humor intensity 0.7+; dialogue speaker IDs resolve; source dialogue is paraphrased unless a user-supplied quote is explicitly locked.

## Comedy Editor

- **Responsibility:** Critique and revise the script without flattening its voice.
- **Input schema:** `ComedyEditRequest {recapScript, compressedPlot, rubric, priorReview?}`.
- **Output schema:** `ComedyEditResult {scores, issues[], edits[], revisedScript, approvalRecommendation}`.
- **System prompt responsibilities:** Remove repetitive premises, punch up weak setups, shorten throat-clearing, verify plot fidelity, make callbacks pay off, and protect the strongest lines.
- **Validation rules:** Score fields are 0 to 100; every edit names a segment and reason; beat coverage cannot fall; revised word count remains within 5%; `APPROVE` requires overall 85+, plot fidelity 92+, and mandatory beat coverage 100%.

## Storyboard Director

- **Responsibility:** Convert measured narration into a timed sequence of visually varied shots.
- **Input schema:** `StoryboardRequest {script, measuredAudioSegments[], episodeAnalysis, visualStyle, providerCapabilities, continuityTimeline}`.
- **Output schema:** `Storyboard` from section 5.
- **System prompt responsibilities:** Show rather than restate narration; plan visual punchlines; limit simultaneous characters; choose shot size, angle, camera motion, action, transition, and start/end frame needs; use exact measured durations.
- **Validation rules:** Full audio coverage with no gap or overlap; shot IDs unique; character/location/state references valid; desired durations fit the retimer rules; at least one visual change every 8 seconds; no cut within a protected punchline window.

## Visual Prompt Engineer

- **Responsibility:** Compile a validated ShotDefinition and reference bundle into provider-neutral visual intent and provider-specific prompts.
- **Input schema:** `VisualPromptRequest {shot, characterReferences[], locationReference, continuitySnapshot, visualStyle, providerCapabilities}`.
- **Output schema:** `PromptPackage {neutralIntent, imagePrompt, motionPrompt, negativeConstraints[], referenceBindings[], seedPolicy, providerOverrides}`.
- **System prompt responsibilities:** Put identity and state invariants first; express one primary action; separate composition from motion; use reference bindings by ID; avoid conflicting camera verbs and accidental text.
- **Validation rules:** Every visible character has a reference and state; prompt contains exact count and location; one camera movement maximum; negative constraints are supported by the selected adapter; prompt length is below provider maximum.

## Character Continuity Agent

- **Responsibility:** Resolve each character and location to the exact state that applies at a shot time, then detect contradictions.
- **Input schema:** `ContinuityRequest {shot, precedingShots[], characterDefinitions[], stateEvents[], locationDefinitions[]}`.
- **Output schema:** `ContinuitySnapshot {shotId, characters[], location, invariants[], allowedChanges[], conflicts[]}`.
- **System prompt responsibilities:** Apply state events in chronological order; distinguish persistent from scene-local changes; never infer a wardrobe or injury reset; compare against adjacent locked shots.
- **Validation rules:** One active state per state category per character; state interval covers shot; changes cite an event; blocking conflicts prevent prompt generation.

## Voice Director

- **Responsibility:** Produce delivery instructions and pronunciation data for each narration or dialogue segment.
- **Input schema:** `VoiceDirectionRequest {scriptSegment, voiceProfile, adjacentSegments[], targetDurationMs, pronunciationLexicon}`.
- **Output schema:** `VoiceDirection {voiceId, model, instructions, paceWpm, pronunciations[], pausePlan[], emotion, gainDb}`.
- **System prompt responsibilities:** Keep narrator identity stable; vary emphasis with jokes; avoid overacting; make quoted dialogue distinguishable; fit target duration without sounding artificially sped up.
- **Validation rules:** Voice ID is approved; pace is 125 to 195 WPM unless overridden; pause total fits duration; all nonstandard names receive pronunciation hints; instructions contain no script changes.

## Visual QA Agent

- **Responsibility:** Compare sampled video frames and motion evidence with the shot contract and canonical references.
- **Input schema:** `VisualQARequest {shotDefinition, frames[], motionSummary, characterReferences[], locationReference, adjacentFrameRefs[]}`.
- **Output schema:** `QAResult` from section 5.
- **System prompt responsibilities:** Score only visible evidence; identify exact frame times; distinguish uncertain from wrong; emit machine-actionable failure codes and repair instructions.
- **Validation rules:** All rubric dimensions present; weighted score recomputes exactly; hard failures follow policy; every failure cites at least one evidence frame; `PASS` requires score threshold and no hard failure.

## Final Editorial QA Agent

- **Responsibility:** Review the completed timeline for story clarity, comedy pacing, continuity, A/V sync, caption correctness, and publication readiness.
- **Input schema:** `FinalQARequest {lowResolutionFinal, renderReport, script, storyboard, shotQAResults, captions, publicationMetadata}`.
- **Output schema:** `FinalQAResult {scores, timelineIssues[], blockingIssues[], recommendedFixes[], verdict}`.
- **System prompt responsibilities:** Judge the final experience, not individual assets in isolation; detect repeated visuals, joke collisions, music masking, dead air, continuity jumps, and misleading metadata.
- **Validation rules:** `APPROVE` requires no blocking issues, A/V sync within 80 ms, caption text match above 99.5%, no missing mandatory beat, overall 88+, and technical render checks passing.

# 4. Data Model

PostgreSQL stores relational truth and lightweight JSON state. Large bytes live in Blob Storage. Provider responses are retained as redacted JSON in `generation_attempts`. The DDL below is sufficient to start development.

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    status text NOT NULL DEFAULT 'UPLOADED',
    target_duration_ms integer NOT NULL CHECK (target_duration_ms BETWEEN 30000 AND 900000),
    humor_intensity numeric(3,2) NOT NULL CHECK (humor_intensity BETWEEN 0 AND 1),
    visual_style jsonb NOT NULL,
    narrator_profile jsonb,
    settings jsonb NOT NULL DEFAULT '{}',
    current_analysis_version integer,
    current_script_version integer,
    current_storyboard_version integer,
    row_version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind text NOT NULL,
    blob_uri text NOT NULL,
    sha256 char(64) NOT NULL,
    mime_type text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    duration_ms integer,
    width integer,
    height integer,
    frame_rate numeric(8,3),
    parent_asset_ids uuid[] NOT NULL DEFAULT '{}',
    provenance jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, sha256, kind)
);

CREATE TABLE source_videos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    original_asset_id uuid NOT NULL REFERENCES assets(id),
    analysis_proxy_asset_id uuid REFERENCES assets(id),
    probe_json jsonb NOT NULL,
    timebase_num integer NOT NULL,
    timebase_den integer NOT NULL,
    duration_ms integer NOT NULL,
    ingest_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE characters (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    analysis_version integer NOT NULL,
    canonical_name text NOT NULL,
    aliases text[] NOT NULL DEFAULT '{}',
    role text NOT NULL,
    importance numeric(4,3) NOT NULL CHECK (importance BETWEEN 0 AND 1),
    description text NOT NULL,
    visual_bible jsonb NOT NULL,
    voice_profile jsonb,
    source_refs jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id, analysis_version, canonical_name)
);

CREATE TABLE character_states (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id uuid NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    category text NOT NULL,
    state_key text NOT NULL,
    state_payload jsonb NOT NULL,
    valid_from_ms integer NOT NULL,
    valid_to_ms integer,
    caused_by_scene_id uuid,
    source_refs jsonb NOT NULL,
    CHECK (valid_to_ms IS NULL OR valid_to_ms > valid_from_ms)
);

CREATE TABLE locations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    analysis_version integer NOT NULL,
    canonical_name text NOT NULL,
    description text NOT NULL,
    invariant_layout jsonb NOT NULL,
    visual_palette jsonb NOT NULL,
    source_refs jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id, analysis_version, canonical_name)
);

CREATE TABLE scenes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    analysis_version integer NOT NULL,
    sequence_no integer NOT NULL,
    source_start_ms integer NOT NULL,
    source_end_ms integer NOT NULL,
    location_id uuid REFERENCES locations(id),
    summary text NOT NULL,
    dramatic_purpose text,
    transcript_span_ids uuid[] NOT NULL DEFAULT '{}',
    representative_asset_ids uuid[] NOT NULL DEFAULT '{}',
    source_refs jsonb NOT NULL,
    CHECK (source_end_ms > source_start_ms),
    UNIQUE(project_id, analysis_version, sequence_no)
);

ALTER TABLE character_states
    ADD CONSTRAINT fk_character_state_scene
    FOREIGN KEY (caused_by_scene_id) REFERENCES scenes(id);

CREATE TABLE scene_characters (
    scene_id uuid NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    character_id uuid NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    speaker_label text,
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY(scene_id, character_id)
);

CREATE TABLE plot_beats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    analysis_version integer NOT NULL,
    sequence_no integer NOT NULL,
    beat_type text NOT NULL,
    summary text NOT NULL,
    scene_ids uuid[] NOT NULL,
    character_ids uuid[] NOT NULL DEFAULT '{}',
    depends_on_beat_ids uuid[] NOT NULL DEFAULT '{}',
    importance numeric(4,3) NOT NULL CHECK (importance BETWEEN 0 AND 1),
    payoff_score numeric(4,3) NOT NULL CHECK (payoff_score BETWEEN 0 AND 1),
    mandatory boolean NOT NULL DEFAULT false,
    source_refs jsonb NOT NULL,
    UNIQUE(project_id, analysis_version, sequence_no)
);

CREATE TABLE scripts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version integer NOT NULL,
    status text NOT NULL,
    target_word_count integer NOT NULL,
    actual_word_count integer NOT NULL,
    compressed_plot jsonb NOT NULL,
    review_scores jsonb,
    prompt_version text NOT NULL,
    selected boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id, version)
);
CREATE UNIQUE INDEX one_selected_script ON scripts(project_id) WHERE selected;

CREATE TABLE script_segments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    script_id uuid NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
    sequence_no integer NOT NULL,
    segment_type text NOT NULL CHECK (segment_type IN ('NARRATION','DIALOGUE','PAUSE')),
    speaker_character_id uuid REFERENCES characters(id),
    text text NOT NULL,
    plot_beat_ids uuid[] NOT NULL,
    joke_tags text[] NOT NULL DEFAULT '{}',
    estimated_duration_ms integer NOT NULL,
    measured_duration_ms integer,
    voice_direction jsonb,
    locked boolean NOT NULL DEFAULT false,
    UNIQUE(script_id, sequence_no)
);

CREATE TABLE shots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    storyboard_version integer NOT NULL,
    sequence_no integer NOT NULL,
    script_segment_id uuid NOT NULL REFERENCES script_segments(id),
    source_scene_id uuid REFERENCES scenes(id),
    location_id uuid REFERENCES locations(id),
    edit_start_ms integer NOT NULL,
    edit_duration_ms integer NOT NULL CHECK (edit_duration_ms > 0),
    provider_duration_ms integer NOT NULL CHECK (provider_duration_ms > 0),
    importance text NOT NULL CHECK (importance IN ('UTILITY','NORMAL','HERO')),
    composition jsonb NOT NULL,
    action jsonb NOT NULL,
    continuity_snapshot jsonb NOT NULL,
    prompt_package jsonb,
    status text NOT NULL,
    locked boolean NOT NULL DEFAULT false,
    row_version integer NOT NULL DEFAULT 1,
    UNIQUE(project_id, storyboard_version, sequence_no)
);

CREATE TABLE shot_characters (
    shot_id uuid NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    character_id uuid NOT NULL REFERENCES characters(id),
    character_state_ids uuid[] NOT NULL,
    screen_position text,
    PRIMARY KEY(shot_id, character_id)
);

CREATE TABLE generation_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    shot_id uuid REFERENCES shots(id) ON DELETE CASCADE,
    operation text NOT NULL,
    attempt_no integer NOT NULL,
    input_hash char(64) NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    provider_task_id text,
    provider_config_version text NOT NULL,
    prompt_version text,
    seed bigint,
    request_json jsonb NOT NULL,
    response_json jsonb,
    status text NOT NULL,
    cost_usd numeric(10,4),
    latency_ms integer,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE(operation, input_hash, provider_config_version, attempt_no),
    UNIQUE(provider, provider_task_id)
);

CREATE TABLE generated_images (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shot_id uuid REFERENCES shots(id) ON DELETE CASCADE,
    character_id uuid REFERENCES characters(id) ON DELETE CASCADE,
    location_id uuid REFERENCES locations(id) ON DELETE CASCADE,
    role text NOT NULL,
    generation_attempt_id uuid NOT NULL REFERENCES generation_attempts(id),
    asset_id uuid NOT NULL REFERENCES assets(id),
    selected boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_selected_image_per_role
    ON generated_images(shot_id, role) WHERE selected AND shot_id IS NOT NULL;

CREATE TABLE generated_videos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shot_id uuid NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    generation_attempt_id uuid NOT NULL REFERENCES generation_attempts(id),
    asset_id uuid NOT NULL REFERENCES assets(id),
    generated_duration_ms integer NOT NULL,
    selected boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_selected_video_per_shot
    ON generated_videos(shot_id) WHERE selected;

CREATE TABLE audio_assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    script_segment_id uuid REFERENCES script_segments(id) ON DELETE CASCADE,
    shot_id uuid REFERENCES shots(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('NARRATION','DIALOGUE','SFX','MUSIC','MIX','SOURCE')),
    generation_attempt_id uuid REFERENCES generation_attempts(id),
    asset_id uuid NOT NULL REFERENCES assets(id),
    timing_json jsonb,
    loudness_json jsonb,
    selected boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE qa_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    shot_id uuid REFERENCES shots(id) ON DELETE CASCADE,
    render_job_id uuid,
    target_type text NOT NULL CHECK (target_type IN ('IMAGE','VIDEO','AUDIO','FINAL_RENDER')),
    target_asset_id uuid NOT NULL REFERENCES assets(id),
    evaluator text NOT NULL,
    rubric_version text NOT NULL,
    score numeric(5,2) NOT NULL CHECK (score BETWEEN 0 AND 100),
    hard_failures text[] NOT NULL DEFAULT '{}',
    dimension_scores jsonb NOT NULL,
    evidence jsonb NOT NULL,
    repair_plan jsonb,
    verdict text NOT NULL CHECK (verdict IN ('PASS','FAIL','REVIEW')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE render_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    script_id uuid NOT NULL REFERENCES scripts(id),
    storyboard_version integer NOT NULL,
    input_manifest_asset_id uuid NOT NULL REFERENCES assets(id),
    output_asset_id uuid REFERENCES assets(id),
    attempt_no integer NOT NULL,
    input_hash char(64) NOT NULL,
    status text NOT NULL,
    ffmpeg_version text NOT NULL,
    command_json jsonb NOT NULL,
    report_json jsonb,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id, input_hash, attempt_no)
);

ALTER TABLE qa_results
    ADD CONSTRAINT fk_qa_render_job FOREIGN KEY (render_job_id) REFERENCES render_jobs(id);

CREATE TABLE stage_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage text NOT NULL,
    entity_id uuid NOT NULL,
    input_hash char(64) NOT NULL,
    contract_version text NOT NULL,
    status text NOT NULL,
    temporal_workflow_id text NOT NULL,
    temporal_run_id text,
    attempt_count integer NOT NULL DEFAULT 0,
    output_refs jsonb,
    error_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id, stage, entity_id, input_hash, contract_version)
);

For a project-level stage, `entity_id` is the owning `project_id`. Entity-scoped stages use the
actual entity UUID. This portable non-null convention makes the uniqueness constraint deduplicate
retries consistently in PostgreSQL and SQLite.

CREATE INDEX idx_scenes_project_time ON scenes(project_id, source_start_ms);
CREATE INDEX idx_shots_project_order ON shots(project_id, storyboard_version, sequence_no);
CREATE INDEX idx_attempts_open ON generation_attempts(provider, status, created_at);
CREATE INDEX idx_qa_failed ON qa_results(project_id, verdict, created_at);
```

# 5. Contract Design Sketches

The canonical public contracts are the strict Pydantic models in `src/vidgen/contracts`, with
exported JSON Schemas checked into `packages/contracts/schema` and matching TypeScript contracts.
Those shipped schemas and their validated fixtures are authoritative for field names, types, and
required values.

The JSON blocks below are historical design sketches that preserve intended product semantics.
They predate the shipped snake-case contracts and are not valid v1 fixtures. Implementations must
not submit these camel-case sketches to strict contracts. When implementing a roadmap task, update
or replace its sketch with a fixture generated from the corresponding shipped Pydantic model.

## EpisodeAnalysis

```json
{
  "contractVersion": "1.0.0",
  "episodeId": "episode_01",
  "sourceVideoId": "source_01",
  "title": "The Missing Prototype",
  "durationMs": 2718400,
  "logline": "A cautious engineer loses a prototype and turns a simple search into an office-wide disaster.",
  "genre": ["workplace comedy", "mystery"],
  "tone": ["fast", "absurd", "character-driven"],
  "characters": [
    {
      "characterId": "character_01",
      "canonicalName": "Maya",
      "aliases": ["Maya Chen", "speaker_A"],
      "role": "PROTAGONIST",
      "importance": 0.98,
      "description": "A meticulous engineer whose attempts to stay calm make the situation worse.",
      "firstSceneId": "scene_01",
      "sourceRefs": ["frame_00034", "transcript_0008"]
    },
    {
      "characterId": "character_02",
      "canonicalName": "Derek",
      "aliases": ["speaker_B"],
      "role": "ALLY",
      "importance": 0.76,
      "description": "Maya's confident but consistently incorrect coworker.",
      "firstSceneId": "scene_02",
      "sourceRefs": ["frame_00102", "transcript_0031"]
    }
  ],
  "locations": [
    {
      "locationId": "location_01",
      "canonicalName": "Prototype Lab",
      "description": "Glass-walled electronics lab with a central steel workbench.",
      "invariants": ["glass wall on camera left", "red tool cabinet", "central steel workbench"],
      "sourceRefs": ["frame_00034", "frame_00042"]
    },
    {
      "locationId": "location_02",
      "canonicalName": "Break Room",
      "description": "Small office kitchen with blue cabinets and an overfilled snack rack.",
      "invariants": ["blue cabinets", "snack rack beside refrigerator"],
      "sourceRefs": ["frame_00167"]
    }
  ],
  "scenes": [
    {
      "sceneId": "scene_01",
      "sequence": 1,
      "startMs": 0,
      "endMs": 106000,
      "locationId": "location_01",
      "characterIds": ["character_01"],
      "summary": "Maya locks the prototype in a case before a demonstration.",
      "dramaticPurpose": "SETUP",
      "stateEvents": [
        {
          "eventId": "state_event_01",
          "characterId": "character_01",
          "category": "wardrobe",
          "fromState": null,
          "toState": "blue_jacket",
          "persistent": true,
          "sourceRefs": ["frame_00034"]
        }
      ],
      "sourceRefs": ["transcript_0001", "frame_00034"]
    },
    {
      "sceneId": "scene_02",
      "sequence": 2,
      "startMs": 106000,
      "endMs": 249000,
      "locationId": "location_02",
      "characterIds": ["character_01", "character_02"],
      "summary": "Maya discovers the case is empty and Derek proposes increasingly unlikely suspects.",
      "dramaticPurpose": "INCITING_INCIDENT",
      "stateEvents": [],
      "sourceRefs": ["transcript_0030", "frame_00167"]
    }
  ],
  "plotBeats": [
    {
      "plotBeatId": "beat_01",
      "sequence": 1,
      "type": "SETUP",
      "summary": "Maya secures the prototype for the demonstration.",
      "sceneIds": ["scene_01"],
      "characterIds": ["character_01"],
      "dependsOn": [],
      "importance": 0.86,
      "payoffScore": 0.72,
      "mandatory": true,
      "sourceRefs": ["transcript_0008", "frame_00034"]
    },
    {
      "plotBeatId": "beat_02",
      "sequence": 2,
      "type": "INCITING_INCIDENT",
      "summary": "The prototype vanishes from the locked case.",
      "sceneIds": ["scene_02"],
      "characterIds": ["character_01", "character_02"],
      "dependsOn": ["beat_01"],
      "importance": 1.0,
      "payoffScore": 0.95,
      "mandatory": true,
      "sourceRefs": ["transcript_0030", "frame_00167"]
    }
  ],
  "relationships": [
    {
      "fromCharacterId": "character_01",
      "toCharacterId": "character_02",
      "type": "COWORKERS",
      "description": "Maya trusts Derek's loyalty but not his judgment.",
      "confidence": 0.91,
      "sourceRefs": ["transcript_0031", "transcript_0042"]
    }
  ],
  "unresolved": [
    {
      "id": "ambiguity_01",
      "question": "Whether the off-screen voice in scene 08 is Derek.",
      "candidateAnswers": ["character_02", "unknown_speaker_03"],
      "confidence": 0.53,
      "sourceRefs": ["transcript_0189"]
    }
  ],
  "sourceRefs": ["source_01"],
  "assumptions": [],
  "warnings": []
}
```

## CharacterDefinition

```json
{
  "contractVersion": "1.0.0",
  "characterId": "character_01",
  "canonicalName": "Maya",
  "identityVersion": 3,
  "identityLock": {
    "face": "oval face, high cheekbones, dark brown almond-shaped eyes, straight medium-width nose",
    "skinTone": {"description": "medium warm brown", "paletteHex": ["#8B5A3C", "#6F432E"]},
    "hair": "dark brown shoulder-length curls, center part, consistent silhouette",
    "body": "tall, slim-athletic, long arms, head-to-body ratio 1:7",
    "agePresentation": "early thirties",
    "doNotChange": ["skin undertone", "hair length", "eye shape", "body proportions"]
  },
  "cartoonStyle": {
    "styleId": "style_modern_ink_01",
    "line": "clean dark navy ink, medium weight",
    "shading": "two-step cel shading",
    "texture": "subtle paper grain",
    "proportionMode": "natural with mildly enlarged expressive eyes"
  },
  "defaultWardrobeState": "blue_jacket",
  "wardrobeStates": [
    {
      "stateId": "blue_jacket",
      "description": "cobalt blue cropped work jacket, white crew-neck shirt, charcoal trousers, white sneakers",
      "paletteHex": ["#2456A6", "#F4F1E8", "#33363D"],
      "accessories": ["silver watch on left wrist"],
      "referenceAssetIds": ["asset_maya_blue_front", "asset_maya_blue_side"]
    },
    {
      "stateId": "blue_jacket_torn",
      "description": "same blue jacket with a torn right sleeve and gray dust on the shoulder",
      "paletteHex": ["#2456A6", "#F4F1E8", "#33363D"],
      "accessories": ["silver watch on left wrist"],
      "referenceAssetIds": ["asset_maya_blue_torn"]
    }
  ],
  "expressionReferences": {
    "neutral": "asset_maya_expr_neutral",
    "annoyed": "asset_maya_expr_annoyed",
    "panic": "asset_maya_expr_panic",
    "smug": "asset_maya_expr_smug"
  },
  "turnaroundReferences": {
    "front": "asset_maya_front",
    "leftProfile": "asset_maya_left",
    "rightProfile": "asset_maya_right",
    "back": "asset_maya_back",
    "fullBody": "asset_maya_full"
  },
  "voiceProfile": {
    "provider": "openai",
    "voiceId": "marin",
    "baselineInstructions": "Dry, precise delivery with restrained frustration.",
    "paceWpm": 165
  },
  "sourceRefs": ["frame_00034", "frame_00038", "transcript_0008"],
  "assumptions": [],
  "warnings": []
}
```

## SceneDefinition

```json
{
  "contractVersion": "1.0.0",
  "sceneId": "scene_17",
  "sequence": 17,
  "sourceRange": {"startMs": 1294000, "endMs": 1419000},
  "locationId": "location_03",
  "locationState": "kitchen_night_clean",
  "characterStates": [
    {
      "characterId": "character_01",
      "wardrobeState": "blue_jacket_torn",
      "injuryState": "small_left_cheek_scratch",
      "propState": ["holding_broken_keycard"],
      "emotionStart": "determined",
      "emotionEnd": "embarrassed"
    }
  ],
  "summary": "Maya attempts to reconstruct the broken keycard in the kitchen and accidentally starts the industrial dishwasher.",
  "dramaticPurpose": "ESCALATION",
  "entryState": ["keycard broken", "dishwasher off"],
  "exitState": ["keycard wet", "dishwasher flooding"],
  "plotBeatIds": ["beat_09", "beat_10"],
  "visualMotifs": ["blinking red error light", "rising soap foam"],
  "representativeFrameIds": ["frame_0871", "frame_0890"],
  "sourceRefs": ["transcript_0412", "frame_0871", "frame_0890"],
  "assumptions": [],
  "warnings": []
}
```

## RecapScript

```json
{
  "contractVersion": "1.0.0",
  "scriptId": "script_03",
  "version": 3,
  "targetDurationMs": 300000,
  "targetWordCount": 825,
  "actualWordCount": 821,
  "voiceProfileId": "narrator_dry_01",
  "humorIntensity": 0.7,
  "coldOpenText": "Maya has one job: keep the prototype safe. Naturally, it survives eleven minutes.",
  "segments": [
    {
      "segmentId": "segment_001",
      "sequence": 1,
      "type": "NARRATION",
      "speakerId": "narrator",
      "text": "Maya locks the prototype in a case with more security than the office payroll, then walks away for coffee.",
      "plotBeatIds": ["beat_01"],
      "sourceSceneIds": ["scene_01"],
      "jokes": [
        {"type": "COMPARISON", "setupSpan": [0, 39], "punchlineSpan": [40, 79], "callbackId": null}
      ],
      "visualGag": "The case sprouts increasingly ridiculous locks while a payroll folder sits open beside it.",
      "estimatedDurationMs": 8200,
      "voiceDirection": "Dry confidence, tiny pause before 'then walks away.'",
      "locked": false
    },
    {
      "segmentId": "segment_002",
      "sequence": 2,
      "type": "NARRATION",
      "speakerId": "narrator",
      "text": "She returns to an empty case and Derek, a man whose detective training appears to be three podcasts and a trench coat.",
      "plotBeatIds": ["beat_02"],
      "sourceSceneIds": ["scene_02"],
      "jokes": [
        {"type": "CHARACTER_ROAST", "setupSpan": [0, 42], "punchlineSpan": [43, 113], "callbackId": "derek_detective"}
      ],
      "visualGag": "Derek's imaginary detective credentials fall out of a cereal box.",
      "estimatedDurationMs": 9100,
      "voiceDirection": "Build mild disbelief through the final list.",
      "locked": false
    }
  ],
  "callbacks": [
    {"callbackId": "derek_detective", "setupSegmentId": "segment_002", "payoffSegmentId": "segment_021", "description": "Derek finally solves something by accident."}
  ],
  "beatCoverage": [
    {"plotBeatId": "beat_01", "segmentIds": ["segment_001"], "coverage": "FULL"},
    {"plotBeatId": "beat_02", "segmentIds": ["segment_002"], "coverage": "FULL"}
  ],
  "sourceRefs": ["beat_01", "beat_02"],
  "assumptions": [],
  "warnings": []
}
```

## Storyboard

```json
{
  "contractVersion": "1.0.0",
  "storyboardId": "storyboard_04",
  "version": 4,
  "scriptId": "script_03",
  "audioMasterAssetId": "asset_narration_master_03",
  "durationMs": 300000,
  "frameRate": 24,
  "aspectRatio": "16:9",
  "shots": [
    {
      "shotId": "shot_001",
      "sequence": 1,
      "scriptSegmentId": "segment_001",
      "editStartMs": 0,
      "editDurationMs": 4200,
      "providerDurationMs": 5000,
      "importance": "NORMAL",
      "visualPurpose": "Establish Maya's excessive security ritual.",
      "locationId": "location_01",
      "characterIds": ["character_01"],
      "composition": {"shotSize": "MEDIUM_WIDE", "angle": "EYE_LEVEL", "cameraMove": "SLOW_DOLLY_IN"},
      "action": "Maya snaps three oversized locks onto a small silver case.",
      "transitionIn": "CUT",
      "transitionOut": "MATCH_CUT",
      "requiresFirstFrame": true,
      "requiresLastFrame": false
    },
    {
      "shotId": "shot_002",
      "sequence": 2,
      "scriptSegmentId": "segment_001",
      "editStartMs": 4200,
      "editDurationMs": 4000,
      "providerDurationMs": 5000,
      "importance": "UTILITY",
      "visualPurpose": "Land the payroll comparison and exit action.",
      "locationId": "location_01",
      "characterIds": ["character_01"],
      "composition": {"shotSize": "INSERT", "angle": "HIGH_ANGLE", "cameraMove": "PAN_RIGHT"},
      "action": "Camera passes the absurdly locked case and reveals an open payroll folder while Maya leaves with coffee.",
      "transitionIn": "MATCH_CUT",
      "transitionOut": "CUT",
      "requiresFirstFrame": true,
      "requiresLastFrame": true
    }
  ],
  "timingValidation": {"audioDurationMs": 300000, "visualDurationMs": 300000, "driftMs": 0},
  "sourceRefs": ["script_03", "asset_narration_master_03"],
  "assumptions": [],
  "warnings": []
}
```

## ShotDefinition

```json
{
  "contractVersion": "1.0.0",
  "shotId": "shot_031",
  "sequence": 31,
  "scriptSegmentId": "segment_014",
  "sourceSceneId": "scene_17",
  "editTiming": {"startMs": 132400, "durationMs": 5600, "protectedAudioTailMs": 180},
  "generationTiming": {"requestedMs": 6000, "trimInMs": 0, "trimOutMs": 400},
  "importance": "HERO",
  "visualPurpose": "Escalate the failed keycard repair and land the dishwasher gag.",
  "location": {
    "locationId": "location_03",
    "stateId": "kitchen_night_clean",
    "referenceAssetIds": ["asset_kitchen_wide", "asset_kitchen_reverse"],
    "requiredInvariants": ["blue cabinets", "dishwasher under rear counter", "snack rack camera right"]
  },
  "characters": [
    {
      "characterId": "character_01",
      "identityVersion": 3,
      "referenceAssetIds": ["asset_maya_front", "asset_maya_blue_torn", "asset_maya_expr_panic"],
      "wardrobeState": "blue_jacket_torn",
      "injuryState": "small_left_cheek_scratch",
      "propState": ["holding_broken_keycard"],
      "emotion": "panic concealed as concentration",
      "screenPosition": "CENTER_LEFT"
    }
  ],
  "composition": {
    "shotSize": "MEDIUM",
    "cameraAngle": "EYE_LEVEL",
    "lensEquivalentMm": 40,
    "cameraMotion": "SLOW_PUSH_IN",
    "subjectPriority": ["Maya's face", "broken keycard", "dishwasher warning light"]
  },
  "action": {
    "startPose": "Maya aligns two wet halves of a keycard over the counter.",
    "primaryAction": "The dishwasher erupts with foam behind her while she keeps concentrating.",
    "endPose": "Maya slowly looks over her shoulder as foam reaches her elbow.",
    "motionIntensity": 0.55
  },
  "style": {
    "styleId": "style_modern_ink_01",
    "rendering": "clean dark-navy line art, two-step cel shading, subtle paper grain",
    "palette": "cool kitchen blues contrasted with warm skin and red warning light"  },
  "audioCues": [
    {"offsetMs": 2900, "type": "SFX", "cue": "dishwasher alarm chirp"},
    {"offsetMs": 3600, "type": "SFX", "cue": "soft foam burst"}
  ],
  "negativeConstraints": ["no extra people", "no readable text", "no wardrobe changes", "no extra fingers", "no camera shake"],
  "seedPolicy": {"mode": "REUSE_IF_IDENTITY_PASSES", "preferredSeed": 71933411},
  "sourceRefs": ["scene_17", "frame_0871", "asset_maya_blue_torn"],
  "assumptions": [],
  "warnings": []
}
```

## QAResult

```json
{
  "contractVersion": "1.0.0",
  "qaResultId": "qa_shot_031_attempt_02",
  "shotId": "shot_031",
  "targetAssetId": "asset_video_shot_031_attempt_02",
  "attempt": 2,
  "rubricVersion": "visual-shot/2.1.0",
  "dimensionScores": {
    "characterIdentity": {"score": 23, "max": 25, "confidence": 0.94},
    "characterCount": {"score": 10, "max": 10, "confidence": 0.99},
    "location": {"score": 9, "max": 10, "confidence": 0.91},
    "wardrobeAndState": {"score": 8, "max": 10, "confidence": 0.90},
    "action": {"score": 12, "max": 15, "confidence": 0.86},
    "composition": {"score": 9, "max": 10, "confidence": 0.93},
    "anatomyAndArtifacts": {"score": 9, "max": 10, "confidence": 0.96},
    "continuityAndStyle": {"score": 9, "max": 10, "confidence": 0.89}
  },
  "weightedScore": 89,
  "hardFailures": [],
  "issues": [
    {
      "code": "ACTION_END_POSE_WEAK",
      "severity": "MINOR",
      "description": "Maya begins turning but never completes the over-shoulder look.",
      "evidenceFrameMs": [4800, 5400],
      "repairHint": "Start the head turn 0.8 seconds earlier and reduce foam motion during the final second."
    }
  ],
  "deterministicChecks": {
    "durationValid": true,
    "resolutionValid": true,
    "blackFrameRatio": 0.0,
    "freezeFrameRatio": 0.04,
    "ocrUnexpectedText": [],
    "faceTrackContinuity": 0.93
  },
  "verdict": "PASS",
  "threshold": 85,
  "requiresHumanReview": false,
  "recommendedAction": "ACCEPT",
  "sourceRefs": ["frame_sample_031_00", "frame_sample_031_48", "asset_maya_front"],
  "assumptions": [],
  "warnings": []
}
```

# 6. State Machine

The authoritative state is the Temporal event history. `projects.status` is a read-optimized projection for the UI. No client may set it directly.

```text
UPLOADED
  -> INGESTING -> INGESTED
  -> TRANSCRIBING -> TRANSCRIBED
  -> ANALYZING -> ANALYZED
  -> SCRIPTING -> SCRIPT_READY
  -> SCRIPT_REVIEW [wait for signal when required]
  -> VOICE_GENERATION -> VOICE_READY
  -> STORYBOARDING -> STORYBOARD_READY
  -> REFERENCE_GENERATION -> REFERENCES_READY
  -> ASSET_GENERATION -> ANIMATING
  -> SHOT_QA -> SHOTS_READY
  -> AUDIO_DESIGN -> RENDERING
  -> FINAL_QA
  -> HUMAN_REVIEW
  -> COMPLETE
  -> PUBLISHING -> PUBLISHED
```

Every active state has three projections:

- `*_RETRY_WAIT`: a retryable activity failed and has a scheduled backoff.
- `*_BLOCKED`: a deterministic precondition, missing user decision, or quota prevents progress.
- `*_FAILED`: retries are exhausted or the failure is non-retryable.

Allowed recovery commands are `RETRY_STAGE`, `RETRY_ENTITY`, `CHANGE_PROVIDER`, `EDIT_INPUT`, `SKIP_OPTIONAL`, `APPROVE_FALLBACK`, and `CANCEL_PROJECT`. Each is a Temporal Update with an idempotency key and optimistic row version.

## Shot child workflow

```text
DEFINED
  -> PROMPTING
  -> KEYFRAME_GENERATING
  -> KEYFRAME_QA
       PASS -> ANIMATING
       FAIL -> PROMPT_REPAIR -> KEYFRAME_GENERATING
  -> VIDEO_QA
       PASS -> LOCKED
       FAIL -> PROMPT_REPAIR -> ANIMATING
       RETRIES_EXHAUSTED -> FALLBACK_RENDER or HUMAN_REVIEW_REQUIRED
```

The parent waits for all required child workflows, but one failed child does not discard completed siblings. A storyboard edit starts new children only for changed shot hashes. If a script edit changes timing but not visual content, the workflow first attempts deterministic trimming or retiming. It regenerates only shots whose valid duration envelope no longer covers the edit.

## Visual QA scoring and escalation policy

| Dimension | Weight | Examples |
| --- | ---: | --- |
| Character identity | 25 | Face, hair, skin tone, body proportions, identity reference match |
| Character count | 10 | Exact requested number, no duplicated subject |
| Location | 10 | Correct room, landmarks, palette, time-of-day state |
| Wardrobe and character state | 10 | Clothing, accessory, injury, dirt/wetness, held prop |
| Action and motion | 15 | Requested action, start/end poses, motion stability |
| Composition | 10 | Shot size, angle, screen position, camera move |
| Anatomy and artifacts | 10 | Face, hands, limbs, morphing, flicker, random text |
| Continuity and style | 10 | Adjacent-shot match, line work, shading, palette, screen direction |

The application recomputes the weighted total from dimension scores. The model cannot supply an arbitrary final score.

- Utility and normal shots pass at 85 or higher. Hero shots pass at 90 or higher.
- Any hard failure forces `FAIL` regardless of total score.
- A score from 75 to the pass threshold triggers targeted repair. A score below 75 triggers prompt simplification, a new seed, or composition split instead of a cosmetic repair.
- Model confidence below 0.70 on an identity or continuity decision produces `REVIEW` or a Terra adjudication.
- If Luna and Terra verdicts disagree, Terra decides only when its confidence is at least 0.80. Otherwise the shot enters human review.

Hard failures are wrong primary character, wrong character count, a missing mandatory action, severe broken face or anatomy, unintended readable text, a contradictory persistent state, duration error over 200 ms, black video, decode failure, or A/V material that cannot be normalized.

Deterministic warning thresholds include black frames above two consecutive frames, freeze ratio above 35% unless the shot calls for stillness, unexpected OCR confidence above 0.80, face-track continuity below 0.75, and perceptual style distance beyond the calibrated project threshold.

The creative budget is the initial generation plus two same-provider repairs. A third creative attempt uses one alternate provider when its estimated cost remains inside the shot cap. Exhaustion produces a deterministic 2.5D fallback for Utility/Normal shots or `HUMAN_REVIEW_REQUIRED` for Hero shots. Transport retries, polling interruptions, and output-download retries do not consume creative attempts.

## Error classes

| Class | Examples | Temporal policy |
| --- | --- | --- |
| Transient | 429, 5xx, connection reset, provider task temporarily unavailable | Exponential retry with jitter; preserve remote task ID; maximum elapsed time 30 minutes for LLM/TTS and 2 hours for video |
| Capacity | Provider concurrency full, Azure job capacity, daily generation cap | Long backoff or wait on quota signal; do not consume creative retry budget |
| Content or provider rejection | Prompt rejected, unsupported parameter, safety filter | Non-transient; repair prompt once or route to approved alternate provider |
| Contract | Invalid JSON, missing reference, duration sum mismatch | One schema repair for agent output; deterministic violations return to producing stage; never retry identical input |
| Quality | Identity drift, artifacts, wrong action | Shot-local creative retry budget |
| Human decision | Ambiguous speaker, canonical reference choice, final approval | Workflow waits indefinitely with periodic Continue-As-New if history grows |
| Fatal input | Corrupt media, no audio, encrypted file without decoder | `NEEDS_INPUT`; user replaces the source or cancels |

# 7. Character Consistency System

## 7.1 Identity is a versioned asset, not a paragraph

Each character has an `identityVersion`. A version consists of:

1. An identity lock with measurable traits: face geometry, skin-tone palette, hair silhouette, body proportions, age presentation, and invariant accessories.
2. Five or more clean source crops selected across lighting and angles.
3. A stylized turnaround: front, both profiles, back, three-quarter, and full-body.
4. A face sheet with neutral plus at least six expressions.
5. A wardrobe catalog. Each wardrobe state has front and side references, palette values, accessories, and valid time intervals.
6. An embedding set computed from both source and approved stylized references. Embeddings are supporting evidence, not the sole identity gate.
7. A canonical prompt fragment and a list of features that must not change.

Approved references are immutable. Improving Maya's sheet creates identity version 4. Existing locked shots retain version 3 until a reviewer explicitly migrates them. This prevents one late reference edit from silently altering an entire episode.

## 7.2 Reference selection

The reference builder ranks source frames using:

```text
score = 0.30 face_resolution
      + 0.20 sharpness
      + 0.15 frontal_or_required_angle
      + 0.15 unobstructed_ratio
      + 0.10 neutral_lighting
      + 0.10 expression_usefulness
      - subtitle_penalty
      - motion_blur_penalty
```

It clusters near-duplicates, then chooses coverage across angle, expression, lighting, and wardrobe. A human can replace any candidate before the stylized identity is locked.

## 7.3 Style lock

The project owns a machine-readable `StyleDefinition`, including line weight, outline color, shading steps, texture, saturation, anatomy exaggeration, eye scale, background detail, lighting, lens language, motion intensity, and prohibited features. A generated style board contains the same neutral subject and location in six representative shot types. Every character and location sheet references one approved `styleId` and `styleVersion`.

Do not describe style only as a loose phrase such as "Saturday morning cartoon." Compile it into stable tokens such as:

```json
{
  "styleId": "style_modern_ink_01",
  "version": 2,
  "line": {"color": "#172033", "weight": "medium", "variation": "low"},
  "shading": {"method": "cel", "steps": 2, "softGradients": false},
  "proportions": {"mode": "natural", "eyeScale": 1.12, "handScale": 1.0},
  "texture": {"paperGrain": 0.08},
  "background": {"detail": "medium", "perspective": "consistent"},
  "motion": {"defaultIntensity": 0.45, "cameraShake": false}
}
```

## 7.4 Event-sourced character state

State is resolved from chronological events. Categories include `wardrobe`, `injury`, `dirt`, `wetness`, `held_prop`, `hair`, `age`, `transformation`, and `emotion`. Persistent categories remain active until an explicit change. Emotion is normally shot-local. Held props may be scene-local or persistent based on the event.

```json
{
  "eventId": "state_event_44",
  "characterId": "character_01",
  "category": "injury",
  "fromState": "none",
  "toState": "small_left_cheek_scratch",
  "effectiveFromMs": 1184000,
  "effectiveUntilMs": null,
  "persistent": true,
  "causedBySceneId": "scene_15",
  "sourceRefs": ["frame_0761", "transcript_0380"]
}
```

The continuity resolver folds all events at the shot's source time and emits the complete snapshot:

```json
{
  "characterId": "character_01",
  "identityVersion": 3,
  "wardrobeState": "blue_jacket_torn",
  "injuryState": "small_left_cheek_scratch",
  "heldProps": ["broken_keycard"],
  "locationId": "location_03",
  "sceneId": "scene_17"
}
```

If the recap reorders source beats, the shot carries both `sourceTimeMs` for canonical state and `editTimeMs` for the final timeline. The source time controls continuity unless the storyboard explicitly declares a montage or hypothetical gag.

## 7.5 Location continuity

Each recurring location has:

- A wide establishing sheet, reverse angle, and two corner views.
- A simple 2D floor plan that fixes doors, windows, major furniture, and landmark positions.
- Palette and material swatches.
- Day, night, clean, damaged, flooded, or other explicit state variants.
- Camera-side rules, such as the glass wall remaining screen-left in the default axis.

The prompt compiler includes only the references needed for the current camera angle. Overloading a provider with every sheet can reduce adherence. The location QA compares landmarks, palette, and left/right placement against the relevant angle.

## 7.6 Prompt compilation order

The provider-neutral prompt is compiled in this order:

1. Project style lock.
2. Exact character count and identity bindings.
3. Character states and wardrobe.
4. Location and location-state invariants.
5. Composition and lens.
6. One primary action plus start and end pose.
7. Camera motion.
8. Lighting and mood.
9. Negative constraints.

References are bound by ID and role, for example `character_01_face`, `character_01_wardrobe`, and `location_03_wide`. The adapter translates bindings into the provider's image slots. The database stores the exact list and order because some providers are sensitive to ordering.

## 7.7 Seeds and generation strategy

- Reuse a seed when identity and composition pass but motion or a small artifact fails.
- Change the seed when anatomy, count, or composition is structurally wrong.
- Prefer editing an approved keyframe over generating from scratch for recurring characters.
- Generate complex group shots from a blocked composition image: place simplified silhouettes first, then use reference editing to resolve identities.
- Limit normal shots to two visible speaking characters. Use inserts and reaction cuts instead of demanding four-character choreography from one generation.
- For recurring hero characters at production scale, add an optional character-specific adapter using a trained LoRA or provider consistency feature. Keep that adapter behind the same `ImageGenerator` interface.

## 7.8 Cross-shot continuity QA

The Visual QA Agent receives the last approved frame of the preceding shot and the first frame of the following shot when available. It checks:

- Identity embedding and descriptive traits.
- Wardrobe palette and accessories.
- Injury, dirt, wetness, and held props.
- Screen direction and location axis.
- Time-of-day and location damage state.
- Style embedding, line density, saturation, and background detail.

A shot can pass standalone QA and still fail continuity QA. Continuity failure regenerates the cheaper side of the cut unless one side is locked. Locked assets are never changed automatically.

# 8. Prompt Templates

Templates are stored as versioned files and rendered with strict variable allowlists. The model is always instructed to return the named JSON Schema through Structured Outputs.

## 8.1 Episode analysis

```text
SYSTEM
You are the Episode Analyst in a media pipeline. Build a factual canonical
representation from supplied evidence. Preserve chronology. Every character,
location, scene, state change, relationship, and plot beat must cite sourceRefs.
Distinguish direct observation from inference. Record ambiguity in unresolved.
Do not write recap prose or jokes. Do not invent events, names, or motives.
Merge aliases only when evidence supports the merge. Return EpisodeAnalysis v1.

USER
Project: {{project_id}}
Source duration: {{source_duration_ms}}
Transcript segments: {{transcript_segments}}
Speaker turns: {{speaker_turns}}
Detected scenes: {{detected_scenes}}
Representative frames: {{frame_manifest}}
Optional references: {{user_references}}

Validation emphasis:
- Scene ranges must be monotonic and inside the source duration.
- State events must say whether they persist.
- Plot beats must form a causal dependency graph.
- Use stable IDs with the supplied ID prefixes.
```

## 8.2 Plot compression

```text
SYSTEM
You are the Plot Compressor. Select a causally complete recap plan that fits an
exact narration budget. Preserve setup, inciting incident, escalation, climax,
and resolution. Prefer beats that cause or pay off later beats. Never add an
event. Explain how omitted subplots affect comprehension. Return
CompressedPlotPlan v1, not narration.

USER
Episode analysis: {{episode_analysis}}
Target duration ms: {{target_duration_ms}}
Narration rate WPM: {{target_wpm}}
Maximum words: {{target_words}}
Required beat IDs: {{required_beat_ids}}
Recap mode: {{recap_mode}}

Allocate words per selected beat. The allocation must sum to target_words.
Order must respect every dependsOn relationship.
```

## 8.3 Comedy writing

```text
SYSTEM
You are the Comedy Writer. Write an original, performable comedy recap from the
approved plot plan. Facts and event order are constraints. Humor may come from
commentary, analogy, escalation, visual exaggeration, callbacks, contrast, and
character-specific observations. Vary joke mechanisms. Keep setups short and
put strong words near punchline endings. Tag every segment with plotBeatIds,
sourceSceneIds, joke metadata, visualGag, duration estimate, and voice direction.
Return RecapScript v1. Do not describe your reasoning.

USER
Compressed plot: {{compressed_plot}}
Character facts: {{character_facts}}
Channel voice: {{channel_voice}}
Humor intensity 0..1: {{humor_intensity}}
Target words: {{target_words}}
Target duration ms: {{target_duration_ms}}
Avoid patterns: {{prohibited_patterns}}
Existing callback registry: {{callback_registry}}
```

## 8.4 Comedy editing

```text
SYSTEM
You are the Comedy Editor. Score the script for plot fidelity, clarity, joke
density, joke variety, spoken rhythm, pacing, callbacks, and repetition. Protect
strong lines. Repair only demonstrated weaknesses. Every edit must identify the
segment, old text, new text, and reason. Return ComedyEditResult v1 with the full
revised script. APPROVE requires overall >= 85, plot fidelity >= 92, and all
mandatory beats covered. Do not introduce new plot facts.

USER
Plot plan: {{compressed_plot}}
Draft script: {{recap_script}}
Rubric: {{comedy_rubric}}
Prior review: {{prior_review}}
```

## 8.5 Storyboarding

```text
SYSTEM
You are the Storyboard Director. Cover the measured narration with visually
clear, varied, animation-feasible shots. Show visual jokes instead of merely
illustrating every noun. Each shot must specify purpose, exact edit timing,
provider duration, characters, canonical states, location, composition, one
primary action, transitions, and reference needs. Split long narration at
semantic boundaries. Respect provider capabilities. Return Storyboard v1.

USER
Script: {{recap_script}}
Measured narration: {{measured_audio_segments}}
Episode model: {{episode_analysis}}
Continuity timeline: {{continuity_timeline}}
Visual style: {{visual_style}}
Provider capabilities: {{provider_capabilities}}
Timing rules: {{timing_rules}}
```

## 8.6 Character sheet generation

```text
Create one production character reference image for {{character_name}}.

IDENTITY REFERENCE
{{character_reference}}

IDENTITY LOCK
{{identity_lock}}

WARDROBE STATE
{{wardrobe_state}}

VISUAL STYLE
{{visual_style}}

SHEET TYPE
{{sheet_type}}

Requirements: exact same person in every panel; stable skin tone, face geometry,
hair silhouette, body proportions, outfit, and accessories; neutral studio
lighting; uncluttered light-gray background; full figure fully inside frame;
clear separation between views; no labels, letters, logos, watermarks, borders,
or extra people. This is a consistency asset, not a dramatic scene.
```

## 8.7 Shot image generation

```text
Generate the first keyframe for one 16:9 animated comedy shot.

STYLE LOCK
{{visual_style}}

CHARACTER REFERENCES
{{character_reference}}
Visible character count: {{character_count}}
Character states: {{character_states}}

LOCATION REFERENCE
{{location_reference}}
Required location invariants: {{location_invariants}}

SCENE CONTEXT
{{scene_context}}

COMPOSITION
{{camera_composition}}

PRIMARY ACTION AND START POSE
{{shot_action}}

Preserve exact identity, skin tone, hair, proportions, wardrobe, accessories,
injury state, props, location layout, and cartoon rendering. Make the pose
animation-ready with readable silhouettes and clear subject separation.
Do not include: {{negative_constraints}}. No readable text or watermark.
```

## 8.8 Image-to-video generation

```text
Animate the supplied first frame for {{generation_duration_seconds}} seconds.
Preserve all character identities, facial structure, skin tone, hair, clothing,
body proportions, props, line style, shading, palette, and location geometry.

Action: {{shot_action}}
Start pose: {{start_pose}}
End pose: {{end_pose}}
Camera: {{camera_motion}}
Motion intensity: {{motion_intensity}}
Timing beats: {{motion_timing}}

One coherent action. Natural body mechanics. Stable face and hands. Background
objects remain fixed unless the action names them. No new characters, objects,
text, cuts, zoom jumps, camera shake, morphing, wardrobe changes, or style drift.
{{provider_specific_constraints}}
```

## 8.9 Visual QA

```text
SYSTEM
You are the Visual QA Agent. Compare the sampled frames and motion summary with
the ShotDefinition and canonical references. Score only visible evidence. Cite
frame timestamps for every issue. Apply the exact weighted rubric. Mark a hard
failure for wrong identity, wrong character count, random readable text,
severely broken face/anatomy, missing required action, or a continuity state
contradiction. Return QAResult v1. Do not repair the prompt in this response.

USER
Shot definition: {{shot_definition}}
Character references: {{character_reference}}
Location reference: {{location_reference}}
Previous approved frame: {{previous_frame}}
Sampled frames: {{sampled_frames}}
Motion summary: {{motion_summary}}
Rubric and thresholds: {{qa_rubric}}
```

## 8.10 Prompt repair after QA failure

```text
SYSTEM
You are the Prompt Repair Agent. Make the smallest change that addresses the
observed QA failures. Preserve every constraint that passed. Classify each fix
as prompt, reference binding, seed, composition simplification, duration split,
or provider change. Do not add new creative ideas. Return PromptRepair v1.

USER
Shot definition: {{shot_definition}}
Failed prompt package: {{previous_prompt}}
QA result: {{qa_result}}
Generation metadata: {{generation_metadata}}
Available references: {{available_references}}
Provider capabilities: {{provider_capabilities}}

Required output:
- repairedImagePrompt
- repairedMotionPrompt
- preservedConstraints
- changedConstraints with reasons
- referenceChanges
- seedPolicy
- splitPlan or null
- recommendedProvider
```

# 9. FFmpeg Rendering Pipeline

All commands are generated from a render manifest. Paths are local ephemeral files downloaded by asset ID. Never interpolate user-provided shell text. Use `subprocess.run([...], check=True)` with argument arrays.

## Probe and audio extraction

```bash
ffprobe -v error -show_format -show_streams -of json input.mp4 > probe.json

ffmpeg -hide_banner -y -i input.mp4 \
  -map 0:a:0 -vn -ac 1 -ar 16000 \
  -af "aresample=async=1:first_pts=0" \
  -c:a flac speech.flac

ffmpeg -hide_banner -y -i input.mp4 \
  -map 0:a:0 -vn -ac 2 -ar 48000 \
  -af "aresample=async=1:first_pts=0" \
  -c:a pcm_s24le program.wav
```

## Representative frame extraction

Seek before decode for speed, then choose an exact frame inside the decoded window:

```bash
ffmpeg -hide_banner -y -ss 00:12:34.200 -i analysis-proxy.mp4 \
  -frames:v 1 \
  -vf "scale=1280:-2:flags=lanczos,format=rgb24" \
  frame_scene_17_mid.png
```

For contact sheets:

```bash
ffmpeg -hide_banner -y -i analysis-proxy.mp4 \
  -vf "fps=1/10,scale=320:-2,tile=4x4:padding=4:margin=4" \
  -frames:v 1 contact-sheet.png
```

## Normalize each generated clip

```bash
ffmpeg -hide_banner -y -i shot-031-source.mp4 \
  -t 5.600 \
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,
       pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,
       fps=24,setsar=1,format=yuv420p,setpts=PTS-STARTPTS" \
  -an -c:v libx264 -preset medium -crf 16 -g 48 -keyint_min 48 \
  shot-031-normalized.mp4
```

The command builder removes line breaks from the filter string. For tiny speed corrections, add `setpts=PTS/1.03`; reject values outside `0.95..1.05`.

## Concatenate normalized clips

`concat.txt` is generated by trusted code and contains canonical local paths.

```text
file '/work/shot-001-normalized.mp4'
file '/work/shot-002-normalized.mp4'
file '/work/shot-003-normalized.mp4'
```

```bash
ffmpeg -hide_banner -y -f concat -safe 0 -i concat.txt \
  -c copy picture-master.mp4
```

If transitions are required, build one `filter_complex` with `xfade` offsets computed from the timeline. Do not use crossfades by default because they soften comedy timing.

## Audio mixing and ducking

Inputs are narration, dialogue, SFX, and looped music. Delays come from the audio manifest.

```bash
ffmpeg -hide_banner -y \
  -i narration.wav -i dialogue.wav -i sfx.wav -stream_loop -1 -i music.wav \
  -filter_complex "
    [0:a]aresample=48000,volume=1.0[n];
    [1:a]aresample=48000,volume=1.0[d];
    [n][d]amix=inputs=2:normalize=0:dropout_transition=0[voice];
    [2:a]aresample=48000,volume=0.75[sfx];
    [3:a]aresample=48000,volume=0.22[music];
    [music][voice]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=450[ducked];
    [voice][sfx][ducked]amix=inputs=3:normalize=0:dropout_transition=0,
      alimiter=limit=0.95[mix]" \
  -map "[mix]" -t 300.000 -c:a pcm_s24le premaster.wav
```

In production, each SFX is placed with `adelay`, and the command builder creates labeled branches. Dialogue and narration usually do not overlap.

## Two-pass loudness normalization

Target YouTube delivery is `-14 LUFS` integrated, `-1.5 dBTP`, and LRA at or below 11.

```bash
ffmpeg -hide_banner -i premaster.wav \
  -af "loudnorm=I=-14:LRA=11:TP=-1.5:print_format=json" \
  -f null -
```

Parse `input_i`, `input_tp`, `input_lra`, and `input_thresh`, then run:

```bash
ffmpeg -hide_banner -y -i premaster.wav \
  -af "loudnorm=I=-14:LRA=11:TP=-1.5:
       measured_I={{input_i}}:measured_TP={{input_tp}}:
       measured_LRA={{input_lra}}:measured_thresh={{input_thresh}}:
       linear=true:print_format=json" \
  -ar 48000 -c:a pcm_s24le master-normalized.wav
```

## Captions

Burn ASS captions:

```bash
ffmpeg -hide_banner -y -i picture-master.mp4 \
  -vf "subtitles=captions.ass:fontsdir=/app/fonts" \
  -an -c:v libx264 -preset slow -crf 18 burned-picture.mp4
```

Attach selectable subtitles and final audio:

```bash
ffmpeg -hide_banner -y \
  -i picture-master.mp4 -i master-normalized.wav -i captions.srt \
  -map 0:v:0 -map 1:a:0 -map 2:s:0 \
  -c:v libx264 -preset slow -crf 18 -profile:v high -level 4.2 \
  -pix_fmt yuv420p -r 24 -g 48 \
  -c:a aac -b:a 320k -ar 48000 \
  -c:s mov_text -metadata:s:s:0 language=eng \
  -movflags +faststart -shortest final.mp4
```

## Final deterministic verification

1. `ffprobe` confirms one H.264 1920x1080 video stream, one AAC 48 kHz audio stream, optional subtitle stream, duration within 80 ms, and no negative timestamps.
2. FFmpeg `blackdetect`, `freezedetect`, `silencedetect`, and `ebur128` produce a render report.
3. Decode the entire output to null to catch corrupt frames: `ffmpeg -v error -i final.mp4 -f null -`.
4. Extract a frame every 10 seconds and a waveform preview for final QA.

# 10. API Integration Design

## Provider-neutral interfaces

```python
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

@dataclass(frozen=True)
class AssetRef:
    asset_id: str
    signed_read_url: str
    mime_type: str
    sha256: str

@dataclass(frozen=True)
class ReferenceBinding:
    role: str
    asset: AssetRef
    weight: float = 1.0

@dataclass(frozen=True)
class GenerationContext:
    project_id: str
    entity_id: str
    attempt_id: str
    idempotency_key: str
    trace_id: str

@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    width: int
    height: int
    references: tuple[ReferenceBinding, ...] = ()
    quality: Literal["draft", "standard", "high"] = "standard"
    seed: int | None = None

@dataclass(frozen=True)
class VideoRequest:
    first_frame: AssetRef
    prompt: str
    duration_ms: int
    width: int
    height: int
    references: tuple[ReferenceBinding, ...] = ()
    last_frame: AssetRef | None = None
    seed: int | None = None
    quality_tier: Literal["draft", "standard", "hero"] = "standard"

@dataclass(frozen=True)
class GenerationResult:
    provider: str
    model: str
    provider_task_id: str
    output_urls: tuple[str, ...]
    duration_ms: int | None
    seed: int | None
    cost_usd: float | None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

class ImageGenerator(Protocol):
    async def generate(self, request: ImageRequest, ctx: GenerationContext) -> GenerationResult: ...

class VideoGenerator(Protocol):
    async def generate(self, request: VideoRequest, ctx: GenerationContext) -> GenerationResult: ...
    async def get(self, provider_task_id: str) -> GenerationResult | None: ...
    async def cancel(self, provider_task_id: str) -> None: ...

class VoiceGenerator(Protocol):
    async def synthesize(self, text: str, voice: str, instructions: str,
                         ctx: GenerationContext) -> GenerationResult: ...

class Transcriber(Protocol):
    async def transcribe(self, audio: AssetRef, diarize: bool,
                         ctx: GenerationContext) -> dict[str, Any]: ...
```

## Capability registry

Provider behavior belongs in configuration, not agent prompts:

```yaml
providers:
  runway_gen4_turbo:
    adapter: runway
    model: gen4_turbo
    modes: [image_to_video]
    durations_ms: [5000, 10000]
    ratios: ["1280:768", "768:1280"]
    supports_seed: true
    supports_last_frame: true
    max_references: 1
    estimated_usd_per_second: 0.05
  runway_gen4_5:
    adapter: runway
    model: gen4.5
    modes: [image_to_video, text_to_video]
    duration_range_ms: [2000, 10000]
    duration_step_ms: 1000
    ratios: ["1280:720", "720:1280"]
    supports_seed: true
    supports_last_frame: true
    max_references: 1
    estimated_usd_per_second: 0.12
  veo_3_1_fast:
    adapter: veo
    model: veo-3.1-fast-generate-001
    modes: [image_to_video, first_last_frame]
    durations_ms: [4000, 6000, 8000]
    ratios: ["16:9", "9:16"]
    supports_seed: true
    supports_last_frame: true
    max_references: 3
    estimated_usd_per_second_1080p_video_only: 0.10
```

Runway's current SDK maps image-to-video to `client.image_to_video.create`, returns a task ID, and exposes task retrieval/polling. Do not wait in an HTTP request handler. An activity stores the task ID, then polls with heartbeats or processes a provider callback. [Runway asynchronous task model](https://docs.dev.runwayml.com/api-details/sdks/)

## Example Runway adapter

```python
import asyncio
from runwayml import RunwayML

class RunwayVideoAdapter:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = RunwayML(api_key=api_key)
        self.model = model

    async def generate(self, request: VideoRequest,
                       ctx: GenerationContext) -> GenerationResult:
        attempt = await self._claim_attempt(ctx.idempotency_key)
        if attempt.provider_task_id:
            return await self._poll(attempt.provider_task_id, ctx)
        if attempt.submission_status == "SUBMITTING":
            task_id = await self._reconcile_indeterminate_submission(attempt, ctx)
            if task_id is None:
                raise IndeterminateProviderSubmission(
                    "remote submission outcome is unknown; automatic resubmission is disabled"
                )
            await self._persist_remote_task(ctx, task_id)
            return await self._poll(task_id, ctx)

        seconds = self._supported_seconds(request.duration_ms)
        await self._mark_submitting(attempt, ctx)
        task = await asyncio.to_thread(
            self.client.image_to_video.create,
            model=self.model,
            prompt_image=request.first_frame.signed_read_url,
            prompt_text=request.prompt,
            duration=seconds,
            ratio=f"{request.width}:{request.height}",
            seed=request.seed,
        )
        await self._persist_remote_task(ctx, task.id)
        return await self._poll(task.id, ctx)

    async def _poll(self, task_id: str,
                    ctx: GenerationContext) -> GenerationResult:
        # In production, each loop heartbeats the Temporal activity and persists
        # status. Cancellation calls the provider cancel endpoint.
        task = await asyncio.to_thread(self.client.tasks.retrieve, task_id)
        result = await asyncio.to_thread(task.wait_for_task_output, timeout=900)
        return GenerationResult(
            provider="runway",
            model=self.model,
            provider_task_id=task_id,
            output_urls=tuple(result.output or []),
            duration_ms=None,
            seed=getattr(result, "seed", None),
            cost_usd=None,
            raw_metadata=result.model_dump(),
        )
```

`_claim_attempt` must acquire the unique database claim before submission, and
`_mark_submitting` must commit `SUBMITTING` before the provider call. A timeout or worker death
after that commit is an indeterminate submission, not permission to submit again. The adapter may
resume only after reconciling a provider task through a provider-supported idempotency key,
client-request metadata, callback, or task listing. If reconciliation is unavailable, the attempt
enters operations review and automatic retries stop. This closes the unavoidable local/remote
atomicity gap without risking a duplicate paid job.

The SDK is type annotated, but adapter integration tests must pin and verify its current method signature. Provider API changes are isolated to this module. Runway states that older API versions are supported for four months after a new version, which makes SDK pinning and scheduled compatibility tests necessary. [Runway versioning policy](https://docs.dev.runwayml.com/api-details/versioning/)

## Example Veo adapter outline

```python
class VeoVideoAdapter:
    def __init__(self, client, project: str, location: str,
                 model: str = "veo-3.1-fast-generate-001") -> None:
        self.client = client
        self.endpoint = (
            f"projects/{project}/locations/{location}/"
            f"publishers/google/models/{model}"
        )
        self.model = model

    async def generate(self, request: VideoRequest,
                       ctx: GenerationContext) -> GenerationResult:
        duration_ms = choose_from(request.duration_ms, allowed=(4000, 6000, 8000))
        duration_seconds = duration_ms // 1000
        payload = {
            "prompt": request.prompt,
            "image": await materialize_vertex_image(request.first_frame),
            "durationSeconds": duration_seconds,
            "aspectRatio": "16:9",
            "resolution": "1080p",
            "seed": request.seed,
            "generateAudio": False,
            "storageUri": output_prefix(ctx),
        }
        if request.last_frame:
            payload["lastFrame"] = await materialize_vertex_image(request.last_frame)
        operation = await self.client.predict_async(
            endpoint=self.endpoint,
            instances=[payload],
        )
        await persist_operation_name(ctx, operation.name)
        response = await operation.result(timeout=1800)
        return map_veo_response(response, self.model, operation.name)
```

Veo 3.1 GA supports image-to-video, first/last frame generation, extensions, reference assets, 4/6/8 second clips, and 1080p output. The exact request client may evolve, so `VeoVideoAdapter` owns request mapping and its contract tests call the official test project. [Veo model reference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/veo/3-1-generate)

## Routing policy

```python
def choose_video_provider(shot: ShotDefinition, budget_remaining: float) -> str:
    if shot.importance == "HERO" and budget_remaining >= hero_cost(shot):
        return "runway_gen4_5"
    if shot.requires_multiple_reference_assets:
        return "veo_3_1_fast"
    return "runway_gen4_turbo"
```

Routing is deterministic and versioned. It considers capability, shot importance, remaining project budget, recent provider failure rate, and concurrency. The LLM may recommend a provider but cannot choose one outside the approved registry.

# 11. Queue and Worker Architecture

## Temporal task queues

| Queue | Activities | Scaling unit | Initial concurrency |
| --- | --- | --- | --- |
| `control` | Parent/child workflow code, state projections, dependency invalidation | Always-on lightweight worker | 4 workflow task slots |
| `media-cpu` | FFprobe, audio extraction, scene detection, frames, clip normalization | 2 to 4 vCPU container | 4 activities per replica |
| `llm-analysis` | Episode analysis, compression, writing, editing, storyboarding, prompts | Network worker | 20 activities, provider limited |
| `voice` | TTS, pronunciation retry, narration alignment | Network plus light CPU | 10 activities |
| `image-gen` | Reference and keyframe provider calls | Network worker | Provider quota based, start at 8 |
| `video-gen` | Video task submit, poll/callback, download | Network worker | One permit per provider generation slot |
| `vision-qa` | Frame sampling, OpenCV checks, VLM evaluation | 2 to 4 vCPU container | 8 activities |
| `render` | FFmpeg mix, caption burn, final encode, verification | Dedicated Container Apps Job | 1 render per job container |
| `publish` | YouTube upload and metadata | Network worker | 2 activities |

Workflow and activity payloads contain IDs and small JSON contracts, never raw frames, audio, or full transcripts. Workers load authoritative rows and assets by ID. Outputs are uploaded before a transaction marks the stage complete.

## Idempotency

```text
input_hash = SHA256(
    canonical_json(input_contract)
  + prompt_version
  + contract_version
  + provider_config_version
  + ordered_input_asset_sha256_values
)
```

Before a side effect, an activity inserts or locks the matching `stage_runs` or `generation_attempts` row. Project-level stage runs use `project_id` as their non-null `entity_id`. If a successful output already exists, the activity returns the stored reference. If a provider task ID exists but is incomplete, it resumes polling. A unique database constraint prevents two workers from owning the same creative generation. The activity commits a `SUBMITTING` state before a paid remote call; a retry that finds `SUBMITTING` without a task ID must reconcile the remote outcome or enter operations review, never automatically resubmit.

Provider APIs that support idempotency headers receive `input_hash`. For those that do not, the database claim is acquired with `INSERT ... ON CONFLICT DO NOTHING`, followed by `SELECT ... FOR UPDATE SKIP LOCKED`.

## Retry policies

| Activity | Attempts | Initial interval | Backoff | Max interval | Non-retryable examples |
| --- | ---: | ---: | ---: | ---: | --- |
| Database/blob read/write | 6 | 1 s | 2.0 | 30 s | contract or authorization error |
| OpenAI analysis/TTS | 5 | 2 s | 2.0 | 60 s | invalid request, blocked prompt, schema unchanged after repair |
| Image generation | 4 transport attempts; 2 creative retries | 5 s | 2.0 | 120 s | unsupported input; quality failure is a new attempt, not transport retry |
| Video generation | 6 transport attempts; 2 creative retries plus one fallback | 10 s | 1.8 | 5 min | unsupported duration, prompt rejection |
| FFmpeg | 2 | 2 s | 1.0 | 2 s | corrupt input, missing codec, manifest mismatch |
| YouTube upload | 8 | 5 s | 2.0 | 10 min | invalid OAuth grant, rejected metadata |

All backoff includes 10% to 25% jitter. Temporal activity heartbeats carry the provider task ID, upload block list, or current render phase so cancellation and recovery do not repeat completed work.

## Rate limits and throttling

1. A Redis token bucket exists per `(provider, model, organization)` for requests, tokens, and estimated spend.
2. A weighted semaphore models concurrency. Video calls cost one generation permit; a high-cost 1080p model may cost two budget permits.
3. Workers check the database budget ledger before submission and reserve estimated cost transactionally. They reconcile with actual cost after completion.
4. HTTP 429 updates a provider-specific cooldown key and releases workers back to Temporal instead of busy-waiting.
5. Runway concurrency is tier-dependent and per model. The adapter discovers configured limits and the operator can update them without deploying code. [Runway usage tiers](https://docs.dev.runwayml.com/usage/tiers/)

## Dead-letter handling

Temporal does not require a traditional dead-letter queue for workflow activities. Exhausted failures are explicit workflow states with complete history. The system writes a `pipeline.failure.v1` outbox event and projects it into a `failed-work` operations view.

Azure Service Bus is used for external callbacks, webhook intake, audit export, and optional publication events. Those queues use a native dead-letter queue after ten deliveries. A replay tool validates the event schema and republishes one selected message. It never replays a successful generation blindly.

## Checkpointing

- Audio and transcript checkpoints are per chunk.
- Analysis checkpoints are per source scene batch plus one synthesis version.
- Voice checkpoints are per script segment.
- Image/video/QA checkpoints are per shot and attempt.
- Multipart uploads persist completed block IDs.
- Render checkpoints are the immutable render manifest and normalized shot intermediates.
- Parent workflows use Continue-As-New after large fan-outs or prolonged human waits, passing only stable IDs and current completion sets.

# 12. Cost Model

## Assumptions

- 60-minute source, 5-minute output, 825 narration words.
- 65 final shots averaging 4.62 seconds.
- One character/location reference package and 65 keyframes.
- Costs include normal retries but not human labor.
- Prices are estimates before tax and storage egress. Provider prices can change, so production reads prices from a versioned configuration table.
- Low-cost and recommended profiles may animate at a provider's 720p/768p output and normalize to a 1080p final canvas. The high-quality profile requests native 1080p where available.

Current reference rates include OpenAI text model, transcription, image, speech, and video prices on the [OpenAI pricing page](https://developers.openai.com/api/docs/pricing). Runway sells credits at $0.01 each, with `gen4_turbo` at 5 credits per second and `gen4.5` at 12 credits per second. [Runway pricing](https://docs.dev.runwayml.com/guides/pricing/) Google lists Veo 3.1 Fast video-only at $0.10 per 1080p output-second equivalent and Veo 3.1 standard video-only at $0.20. [Google generative media pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)

## Per-project estimates

| Component | Low-cost | Recommended | High-quality |
| --- | ---: | ---: | ---: |
| Transcription and diarization | $0.40 to $0.80 | $0.50 to $0.90 | $0.50 to $1.20 |
| LLM analysis, script, prompts, QA | $0.75 to $2.00, mostly Luna | $2.00 to $5.00, Terra creative and Luna bulk | $6.00 to $15.00, more Terra/Sol adjudication |
| Character, location, keyframe images | $2.00 to $5.00, standard and fewer candidates | $4.00 to $10.00 | $12.00 to $30.00, high quality and more references |
| Video generation | $9 to $14: animate 50%, 2.5D the rest, 20% retry | $25 to $32: 80% Turbo, 20% Gen-4.5, 30% retry | $68 to $110: Gen-4.5/Veo 3.1 standard, 50% retry |
| Voice | $0.10 to $0.60 | $0.20 to $1.00 | $1.00 to $5.00 with premium voice provider |
| SFX/music generation or catalog amortization | $0 to $1.00 | $1.00 to $3.00 | $3.00 to $8.00 |
| Blob storage, queueing, and render compute | $0.50 to $1.50 | $1.00 to $3.00 | $2.00 to $6.00 |
| **Estimated total** | **$13 to $25** | **$34 to $55** | **$93 to $175** |

### Calculation examples

Low-cost video: `150 generated seconds x 1.20 attempts x $0.05 = $9.00`. Static shots receive layered pans, parallax, crops, and mouth-free reaction motion.

Recommended video: `(240 sec x 1.30 x $0.05) + (60 sec x 1.30 x $0.12) = $24.96`. Allow $25 to $32 for rounding, provider minimums, and unusable outputs.

High-quality video: `(180 sec x 1.50 x $0.12) + (120 sec x 1.50 x $0.20) = $68.40`. More hero-model usage or provider minimums move it toward $110.

## Budget controls

- Project hard cap and warning cap.- Estimated reservation before every paid call.
- Maximum spend per shot by importance: Utility $0.75, Normal $1.50, Hero $4.00 in the recommended profile.
- Automatic switch to 2.5D fallback after creative retries exceed the shot cap.
- Cost ledger fields: provider, model, operation, input/output units, estimated cost, actual cost, attempt ID, shot ID, and reason.

# 13. Performance

## Expected shape

| Metric | Recommended target |
| --- | --- |
| Final duration | 300 seconds, allowed 240 to 360 |
| Narration | 775 to 875 words at 155 to 175 WPM |
| Final shots | 55 to 75, target 65 |
| Average edit duration | 4.0 to 5.5 seconds |
| Generated duration | 5 or 10 seconds on Runway; 4, 6, or 8 seconds on Veo; trim to edit duration |
| Characters visible per normal shot | 1 to 2 |
| Hero shots | 8 to 15 |

## Parallel execution

- Transcription chunks run concurrently after audio splitting. Diarization can run in parallel with canonical transcription.
- Scene frame extraction runs in batches of source timecodes.
- Scene-level analysis runs as a map step, then one global synthesis step.
- Narration segments generate in parallel after a voice-profile calibration sample is approved.
- Character sheets for different characters and location sheets for different locations run concurrently.
- All shots with resolved references can run as child workflows. Provider quotas, not sequence, limit concurrency.
- Visual QA starts as soon as each shot completes. The parent does not wait for all videos before QA.
- Captions, SFX search, and thumbnail concepts can begin while the last shots render.

## Indicative latency

| Phase | Typical wall time with concurrency |
| --- | ---: |
| Ingest, extract, detect, frames | 5 to 15 min |
| Transcription and diarization | 3 to 12 min |
| Analysis, compression, script, edit | 5 to 15 min |
| Voice and measured timing | 2 to 8 min |
| Storyboard and references | 8 to 25 min |
| Keyframes | 5 to 20 min |
| Video generation and shot QA | 20 to 90 min |
| Final sound, render, and QA | 5 to 20 min |
| **End to end without human wait** | **50 to 180 min** |

The principal bottleneck is provider video concurrency and regeneration, not FFmpeg. A second bottleneck is reference approval for new characters. Scale network workers only until provider permits are saturated. Rendering benefits from dedicated CPU and local ephemeral SSD.

# 14. MVP Plan

## MVP 1: Basic automated recap

Scope:

- Upload one MP4 up to 15 minutes.
- Extract audio, transcribe, detect scenes, and extract midpoint frames.
- One Episode Analyst, one Comedy Writer, and one Storyboard Director.
- Generate a 60-second narration, ten keyframes, and ten image-to-video clips.
- Concatenate, mix narration, create SRT, and render 1080p MP4.
- Manual retry button per shot.

Explicit exclusions: speaker-to-character resolution, dialogue voices, advanced state tracking, automatic visual QA, multi-provider routing, YouTube upload, and production autoscaling.

Exit criterion: five golden 10-minute inputs each produce a playable 55 to 70 second MP4 without manual file operations, and a failed shot can be regenerated independently.

## MVP 2: Character consistency

Add character and location definitions, reference selection, stylized sheets, identity versions, wardrobe/state timelines, prompt compiler, continuity snapshots, and reference-aware generation. Add UI steps to approve canonical sheets.

Exit criterion: two recurring characters retain approved identity, wardrobe, skin tone, and hair across 20 shots with at least 85% automated or blinded-review consistency.

## MVP 3: Automatic QA and regeneration

Add deterministic video checks, Visual QA Agent, weighted rubric, prompt repair, two retries, alternate-provider routing, and 2.5D fallback. Add shot-level cost caps and operations view.

Exit criterion: seeded defect tests trigger the correct failure code at least 90% of the time, no hard-fail shot is auto-approved in the golden set, and retry exhaustion reaches human review without blocking unrelated shots.

## MVP 4: Production-scale automation

Add 60+ minute sources, chunked analysis, Temporal Cloud, Azure deployment, provider quota control, review signals, multi-tenant security, publication metadata, optional YouTube upload, cost dashboards, golden evals, and automated retention.

Exit criterion: 20 concurrent projects complete within declared SLOs, no duplicate paid generations occur during worker restarts, and recovery testing demonstrates continuation after API, worker, database, and render interruptions.

# 15. Implementation Roadmap

Each task below is independently testable. File paths refer to the repository structure in section 16.

| ID | Objective | Files, services, or components to create | Dependencies | Definition of done |
| --- | --- | --- | --- | --- |
| T01 | Bootstrap the monorepo and CI | `pyproject.toml`, `pnpm-workspace.yaml`, `Makefile`, `.github/workflows/ci.yml`, base Dockerfiles | None | Python and TypeScript lint, unit tests, and container builds pass in CI |
| T02 | Define versioned contracts | `packages/contracts/src/*.py`, generated JSON Schema, TypeScript types, contract fixtures | T01 | Pydantic round-trip tests pass; TS types generate; invalid fixture suite is rejected |
| T03 | Create the relational model | `packages/db/models.py`, `alembic/versions/0001_core.py`, repositories | T02 | Fresh database migrates up/down; DDL constraints and selected-asset indexes are tested |
| T04 | Implement asset storage and provenance | `packages/storage/blob.py`, `content_address.py`, `asset_service.py` | T03 | Same bytes deduplicate; parent hashes and metadata persist; signed reads expire; upload recovery test passes |
| T05 | Build project and upload API | `apps/api/routes/projects.py`, `uploads.py`, request schemas, auth stub | T02, T04 | Browser can create project, upload MP4 by blocks, finalize, and retrieve probe status |
| T06 | Build deterministic media worker | `services/media_worker/probe.py`, `audio.py`, `frames.py`, `scene_detect.py` | T04, FFmpeg | Golden media yields stable duration, audio, scene boundaries, and frame hashes within tolerances |
| T07 | Implement transcription service | `services/transcription/openai_adapter.py`, `chunker.py`, `overlap_merge.py`, `diarization.py` | T06 | 25 MB chunk limit honored; overlap text deduplicates; timestamps cover at least 98% of voiced audio |
| T08 | Implement Temporal project workflow | `packages/workflows/project.py`, `activities.py`, worker entry point | T03 to T07 | Killing a worker during ingest/transcription resumes without duplicate rows or provider submissions |
| T09 | Implement scene evidence package | `services/analysis/evidence_builder.py`, contact sheet builder, transcript-scene joiner | T06, T07 | Every evidence item has source time and asset ID; oversized payloads are referenced, not embedded |
| T10 | Implement Episode Analyst | `services/analysis/episode_agent.py`, prompt, schema validator, scene-map/global-reduce | T02, T09 | Golden analysis passes FK, chronology, source-ref, and beat dependency checks |
| T11 | Implement compression and comedy script | `services/script/compressor.py`, `writer.py`, `editor.py`, prompts, rubrics | T10 | Mandatory beat coverage 100%; target word count within 5%; revision is versioned and diffable |
| T12 | Implement voice and alignment | `services/voice/openai_tts.py`, `director.py`, `measure.py`, `align.py` | T11 | Per-segment audio regenerates independently; measured durations and word timings persist |
| T13 | Implement storyboard and retimer | `services/storyboard/director.py`, `retimer.py`, provider capability registry | T12 | Shot coverage equals audio duration within 40 ms for continuous and discrete provider fixtures |
| T14 | Implement image generation | `services/image_generation/openai_image.py`, prompt compiler, asset downloader | T04, T13 | A shot creates a keyframe with exact provenance; duplicate activity returns existing output |
| T15 | Implement Runway video adapter | `services/animation/runway.py`, poller, downloader, adapter contract tests | T13, T14 | Remote task ID persists before polling; restart resumes task; output duration and codec are verified |
| T16 | Implement shot child workflow | `packages/workflows/shot.py`, attempt policy, per-shot commands | T14, T15 | Ten shots execute concurrently; one forced failure retries locally and does not rerun siblings |
| T17 | Build captions and render service | `services/renderer/manifest.py`, `captions.py`, `filters.py`, `render.py`, `verify.py` | T12, T16 | Ten-shot fixture renders valid 1080p MP4, AAC audio, selectable SRT, and reproducible report |
| T18 | Build MVP review UI | `apps/web/src/routes/projects/*`, transcript editor, script editor, storyboard grid, shot inspector | T05, T08, T17 | User can upload, monitor, edit script, regenerate one shot, and approve/download final render |
| T19 | Add character/location references | `services/continuity/reference_selector.py`, `bible_builder.py`, `state_resolver.py`, new UI panels | T10, T14, T18 | Approved identity version and state snapshot are injected into every affected shot |
| T20 | Add visual QA | `services/qa/sampler.py`, `deterministic.py`, `visual_agent.py`, rubric and fixtures | T16, T19 | QA score recomputes; hard failures block; evidence timestamps and repair codes persist |
| T21 | Add repair and fallback routing | `services/qa/repair.py`, `services/animation/veo.py`, `services/renderer/parallax.py` | T20 | Two repairs, alternate provider, and deterministic fallback follow policy and cost cap |
| T22 | Add final editorial QA | `services/qa/final_editorial.py`, A/V checks, review gate | T17, T20 | Final workflow cannot complete with a blocking render, caption, continuity, or story issue |
| T23 | Add observability and cost ledger | `packages/telemetry/*`, `packages/costs/*`, dashboards, alerts | T08, provider adapters | Every provider attempt exposes tokens/seconds, cost, latency, trace, result, and failure class |
| T24 | Add Azure infrastructure | `infra/bicep/*` or `infra/terraform/*`, Container Apps manifests, managed identities, Key Vault references | T01, T23 | Staging deploy is private by default, migrations run once, workers autoscale, and secrets are absent from images/config repos |
| T25 | Add YouTube publication | `services/publisher/youtube.py`, OAuth connection UI, resumable upload state | T18, T24 | Private test video, thumbnail, and captions upload once; interrupted upload resumes |
| T26 | Build regression and load suite | `tests/golden/*`, fake providers, chaos tests, load scenarios | All prior | Golden scores are versioned; 20-project load and worker termination tests meet SLO and idempotency requirements |

Recommended execution order is T01 through T18 for the first vertical product, then T19 through T22 for quality, and T23 through T26 for production operations.

# 16. Repository Structure

```text
animated-recap/
  apps/
    api/
      recap_api/
        main.py
        dependencies.py
        routes/
          projects.py
          uploads.py
          transcripts.py
          scripts.py
          storyboards.py
          shots.py
          reviews.py
          publications.py
        auth/
        openapi.py
      tests/
      Dockerfile
    web/
      src/
        app/
        routes/
          projects/
          review/
        components/
          UploadPanel.tsx
          TranscriptEditor.tsx
          ScriptEditor.tsx
          StoryboardGrid.tsx
          ShotInspector.tsx
          TimelinePreview.tsx
          QAResultPanel.tsx
          ApprovalBar.tsx
        api/
        state/
        styles/
      package.json
      vite.config.ts
      Dockerfile

  services/
    media_worker/
      probe.py
      audio.py
      scene_detect.py
      frames.py
      video_checks.py
    transcription/
      service.py
      chunker.py
      overlap_merge.py
      openai_adapter.py
      local_whisper_adapter.py
      diarization.py
    analysis/
      evidence_builder.py
      episode_agent.py
      scene_map.py
      global_reduce.py
    script/
      compressor.py
      writer.py
      editor.py
      rubric.py
    voice/
      director.py
      openai_tts.py
      elevenlabs_tts.py
      pronunciation.py
      align.py
      measure.py
    storyboard/
      director.py
      retimer.py
      timing_validator.py
    continuity/
      reference_selector.py
      bible_builder.py
      location_builder.py
      state_resolver.py
      prompt_compiler.py
    image_generation/
      service.py
      openai_image.py
      vertex_image.py
    animation/
      service.py
      runway.py
      veo.py
      fake.py
    audio_design/
      cue_planner.py
      catalog.py
      mixer_manifest.py
    qa/
      sampler.py
      deterministic.py
      visual_agent.py
      repair.py
      final_editorial.py
    renderer/
      manifest.py
      normalize.py
      filters.py
      captions.py
      parallax.py
      render.py
      verify.py
    publisher/
      youtube.py

  workers/
    temporal_worker/
      main.py
      registry.py
      activity_context.py
      Dockerfile
    render_job/
      main.py
      Dockerfile

  packages/
    contracts/
      src/
        common.py
        episode.py
        character.py
        scene.py
        script.py
        storyboard.py
        shot.py
        qa.py
      jsonschema/
      typescript/
      fixtures/
    workflows/
      project.py
      shot.py
      activities.py
      signals.py
      retry_policies.py
    providers/
      interfaces.py
      capabilities.py
      router.py
      errors.py
    prompts/
      episode_analysis/v1.0.0.txt
      plot_compression/v1.0.0.txt
      comedy_writing/v1.0.0.txt
      comedy_editing/v1.0.0.txt
      storyboarding/v1.0.0.txt
      visual_prompt/v1.0.0.txt
      visual_qa/v1.0.0.txt
      prompt_repair/v1.0.0.txt
    db/
      models.py
      session.py
      repositories/
      alembic/
    storage/
      interfaces.py
      blob.py
      local.py
      content_address.py
    telemetry/
      logging.py
      tracing.py
      metrics.py
    costs/
      ledger.py
      pricing.py
      budget.py
    media/
      ffmpeg.py
      ffprobe.py
      subprocess.py

  infra/
    bicep/
      main.bicep
      modules/
        container_apps.bicep
        postgres.bicep
        storage.bicep
        redis.bicep
        service_bus.bicep
        key_vault.bicep
        monitoring.bicep
    container-apps/
    dashboards/
    policies/

  tests/
    unit/
    integration/
    contracts/
    agent_evals/
    golden/
      episode_short_01/
        expected/
        rubric.yaml
      episode_dialogue_02/
    chaos/
    load/
    fixtures/
      synthetic_media/
      provider_responses/

  tools/
    dev_seed.py
    inspect_manifest.py
    replay_failure.py
    cost_report.py
    render_one_shot.py

  docker-compose.yml
  pyproject.toml
  uv.lock
  pnpm-workspace.yaml
  package.json
  Makefile
  .env.example
  README.md
```

Temporal workflow modules must remain deterministic. They may import contracts, workflow-safe constants, and Temporal APIs, but never provider SDKs, SQLAlchemy sessions, current wall-clock functions, random generators, or FFmpeg helpers. Those belong in activities.

# 17. Local Development Setup

## Prerequisites

```bash
# macOS
brew install ffmpeg uv pnpm

# Verify
ffmpeg -version
uv --version
pnpm --version
docker version
```

Use a checked-in `.tool-versions` or equivalent to pin Python 3.12, Node 22 LTS, FFmpeg 7, pnpm, and the Temporal server image. CI uses the same versions.

## Docker Compose

```yaml
name: animated-recap
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: recap
      POSTGRES_PASSWORD: recap-local
      POSTGRES_DB: recap
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U recap -d recap"]
      interval: 5s
      timeout: 3s
      retries: 20
    volumes: ["postgres-data:/var/lib/postgresql/data"]

  temporal-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: temporal
      POSTGRES_PASSWORD: temporal-local
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U temporal"]
      interval: 5s
      timeout: 3s
      retries: 20
    volumes: ["temporal-data:/var/lib/postgresql/data"]

  temporal:
    image: temporalio/auto-setup:${TEMPORAL_VERSION}
    depends_on:
      temporal-db: {condition: service_healthy}
    environment:
      DB: postgres12
      DB_PORT: 5432
      POSTGRES_USER: temporal
      POSTGRES_PWD: temporal-local
      POSTGRES_SEEDS: temporal-db
    ports: ["7233:7233"]

  temporal-ui:
    image: temporalio/ui:${TEMPORAL_UI_VERSION}
    depends_on: [temporal]
    environment:
      TEMPORAL_ADDRESS: temporal:7233
    ports: ["8088:8080"]

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    ports: ["6379:6379"]
    volumes: ["redis-data:/data"]

  azurite:
    image: mcr.microsoft.com/azure-storage/azurite:${AZURITE_VERSION}
    command: "azurite --blobHost 0.0.0.0 --queueHost 0.0.0.0"
    ports: ["10000:10000", "10001:10001"]
    volumes: ["azurite-data:/data"]

volumes:
  postgres-data:
  temporal-data:
  redis-data:
  azurite-data:
```

Pin actual image versions in `.env.example`, Dependabot, and the release lock file. Do not use `latest` in CI or production.

## Environment variables

```dotenv
APP_ENV=local
LOG_LEVEL=INFO
DATABASE_URL=postgresql+psycopg://recap:recap-local@localhost:5432/recap
REDIS_URL=redis://localhost:6379/0
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
AZURE_STORAGE_CONNECTION_STRING=UseDevelopmentStorage=true
BLOB_CONTAINER_SOURCE=source
BLOB_CONTAINER_ASSETS=assets
BLOB_CONTAINER_RENDERS=renders

OPENAI_API_KEY=
RUNWAYML_API_SECRET=
GOOGLE_CLOUD_PROJECT=
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=
ELEVENLABS_API_KEY=

DEFAULT_REASONING_MODEL=gpt-5.6-terra
DEFAULT_BULK_MODEL=gpt-5.6-luna
DEFAULT_IMAGE_MODEL=gpt-image-2
DEFAULT_TTS_MODEL=gpt-4o-mini-tts
DEFAULT_VIDEO_PROVIDER=runway_gen4_turbo
PROJECT_DEFAULT_BUDGET_USD=55
```

## Start the stack

```bash
cp .env.example .env.local
docker compose --env-file .env.local up -d
uv sync --all-groups
pnpm install --frozen-lockfile
uv run alembic upgrade head

# Terminal 1
uv run uvicorn apps.api.recap_api.main:app --reload --port 8000

# Terminal 2
uv run python -m workers.temporal_worker.main \
  --queues control,media-cpu,llm-analysis,voice,image-gen,video-gen,vision-qa

# Terminal 3
pnpm --filter @recap/web dev
```

Local rendering command:

```bash
uv run python -m workers.render_job.main --render-job-id "$RENDER_JOB_ID"
```

Use fake provider mode for tests and UI development:

```dotenv
PROVIDER_MODE=fake
FAKE_PROVIDER_FIXTURE_DIR=tests/fixtures/provider_responses
```

# 18. Deployment Architecture

## Recommended Azure deployment

```text
Internet
  -> Azure Front Door + WAF
      -> React static app
      -> FastAPI on Azure Container Apps
          -> Temporal Cloud
          -> Azure Database for PostgreSQL Flexible Server
          -> Azure Blob Storage
          -> Azure Managed Redis
          -> Azure Service Bus
          -> Azure Key Vault through managed identity

Temporal task queues
  -> Container Apps worker pools
  -> Container Apps Jobs for isolated FFmpeg renders
  -> External AI/media provider APIs

All services
  -> OpenTelemetry Collector
  -> Application Insights + Log Analytics
```

### What runs where

| Component | Azure placement | Scaling |
| --- | --- | --- |
| React UI | Azure Static Web Apps or static assets behind Front Door | CDN |
| FastAPI control plane | Azure Container Apps, min 2 replicas | HTTP concurrency and CPU |
| Temporal workflows and network activities | Separate Container Apps per queue family | Queue backlog plus provider permits |
| Media CPU workers | Container Apps consumption or dedicated workload profile | Temporal queue depth, max bounded by CPU |
| FFmpeg render | Azure Container Apps Jobs with 4 to 8 vCPU and ephemeral SSD | One execution per render job |
| PostgreSQL | Azure Database for PostgreSQL Flexible Server, zone-redundant production tier | Vertical plus read replica if analytics grows |
| Assets | General-purpose v2 Blob Storage, private endpoints, lifecycle tiers | Effectively independent |
| Cache/limits | Azure Managed Redis | Memory and connection based |
| External event ingress | Azure Service Bus topics and queues | Native throughput units |
| Secrets | Azure Key Vault | Managed service |
| Images | Azure Container Registry | Build pipeline pushes immutable digests |
| Monitoring | Application Insights, Log Analytics, managed dashboards | Managed service |

Azure Container Apps Jobs are appropriate for long-running, event-driven, dedicated containers, including render work. [Azure Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs) The API and workers use managed identities for Azure resources, with Key Vault references for third-party provider secrets. [Container Apps managed identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity), [Container Apps security](https://learn.microsoft.com/en-us/azure/container-apps/security)

## Temporal placement

Use Temporal Cloud for production. It removes the operational burden of a multi-service Temporal cluster while Azure hosts application compute and data. Use mTLS or API-key authentication, one namespace per environment, and separate task queues per workload.

Azure-only alternative: deploy Temporal on AKS with its own PostgreSQL persistence and Elasticsearch/OpenSearch visibility if required. Do not deploy the Temporal server itself as ordinary scale-to-zero Container Apps.

## Network and data path

- API, database, Redis, Blob, Service Bus, and Key Vault use private networking where supported.
- Only Front Door and explicit provider egress are public.
- Workers receive asset IDs, request short-lived user-delegation SAS URLs, and download to per-job ephemeral directories.
- Provider output URLs are copied into Blob Storage immediately, hashed, and no longer treated as durable.
- Use one storage account per environment and separate containers for source, derived, reference, attempt, and final assets.

## Availability and recovery

- PostgreSQL automated backups plus point-in-time recovery.
- Blob soft delete and versioning for control-plane accidents, while application assets remain content-addressed.
- Temporal workflow retention long enough to cover project lifecycle, with application projections retained longer.
- Multi-zone production configuration in one primary region. Restore exercises recreate the environment from infrastructure code and replay from stored contracts and assets.

# 19. Observability

## Structured logs

Every log record uses JSON and includes:

```json
{
  "timestamp": "2026-08-25T14:32:10.412Z",
  "level": "INFO",
  "service": "video-worker",
  "environment": "production",
  "traceId": "01J64Y7F2BDH0FZQMBP0A5W9NE",
  "temporalWorkflowId": "project/0191f1d0",
  "temporalRunId": "run-id",
  "projectId": "0191f1d0-54e0-7c74-9d0b-90bc8c8af101",
  "shotId": "shot_031",
  "attemptId": "attempt_031_02",
  "provider": "runway",
  "model": "gen4_turbo",
  "operation": "image_to_video.poll",
  "durationMs": 4812,
  "status": "SUCCEEDED",
  "costUsd": 0.25
}
```

Never log prompts containing full source transcripts, signed URLs, raw provider keys, OAuth tokens, or source media bytes. Log prompt hashes and prompt versions. A privileged debug store may retain redacted provider requests by attempt ID.

## Metrics

Prometheus/OpenTelemetry metric names:

- `recap_projects_started_total{profile}`
- `recap_projects_completed_total{status}`
- `recap_project_stage_duration_seconds{stage}`
- `recap_stage_failures_total{stage,error_class}`
- `recap_queue_backlog{task_queue}`
- `recap_provider_requests_total{provider,model,operation,status}`
- `recap_provider_latency_seconds{provider,model,operation}`
- `recap_provider_active_generations{provider,model}`
- `recap_provider_rate_limit_total{provider,model}`
- `recap_generation_cost_usd{provider,model,operation}`
- `recap_llm_tokens_total{model,direction,agent}`
- `recap_shot_qa_score{rubric_version}`
- `recap_shot_retry_total{failure_code}`
- `recap_render_realtime_factor`
- `recap_av_drift_ms`
- `recap_human_wait_seconds{gate}`

Use counters for cumulative cost and usage, histograms for latency and scores, and gauges only for active work/backlog.

## Distributed tracing

The project request creates a W3C trace context. The trace crosses FastAPI, Temporal client, activity, provider adapter, Blob operations, QA, and render. Important spans are:

```text
project.workflow
  stage.transcription
    transcription.chunk
  stage.analysis
    agent.episode.scene_batch
    agent.episode.synthesis
  stage.asset_generation
    shot.workflow
      image.generate
      image.qa
      video.generate
      video.qa
  render.final
```

Provider task IDs are span attributes, not trace IDs. Temporal workflow IDs remain searchable even after trace retention expires.

## Dashboards and alerts

Dashboards:

1. Project funnel by state and age.
2. Cost per completed minute, provider, model, and retry reason.
3. Provider p50/p95/p99 latency, error rate, 429 rate, and active permits.
4. QA score distribution and top failure codes.
5. Render time, A/V drift, loudness, and verification failures.
6. Human-review backlog and median wait.

Initial alerts:

- Completion success under 90% over 30 projects.
- Provider error rate over 15% for 10 minutes.
- 429 rate over 5% for 10 minutes.
- p95 shot generation over 20 minutes.
- Actual project cost over 110% of warning budget.
- Any duplicate provider task ID or duplicate selected asset invariant violation.
- Render verification failure rate over 2%.

# 20. Human Review UI

Use React, TypeScript, Fluent UI v9, TanStack Query, and a small local editor state store. Server state remains in the API. Progress arrives through Server-Sent Events keyed by project ID.

## Screens

1. **New project:** drag-and-drop upload, target length, visual style, humor slider, narrator selection, optional reference images, budget profile.
2. **Project dashboard:** stage timeline, elapsed time, estimated remaining time, cost versus cap, current blockers, logs summarized by stage.
3. **Transcript:** synchronized video player, speaker-colored segments, search, rename speaker, edit text, split/merge turn, confidence filter.
4. **Script:** side-by-side compressed beats and recap script, word/timing meter, joke tags, lock segment, regenerate selected segment, version diff.
5. **References:** character/location cards, source candidates, generated sheet variants, approve identity version, edit wardrobe/state descriptions.
6. **Storyboard:** grid and timeline views, narration waveform, shot cards, duration, camera/action, references, drag reorder with timing validation.
7. **Shot inspector:** source evidence, first/last keyframe, generated video attempts, prompt, seed, provider, cost, QA breakdown, regenerate and lock controls.
8. **Final review:** full preview, captions toggle, audio stem meters, issue markers, title/thumbnail choices, approve render, request targeted fixes.

## Minimal API surface

```text
POST   /v1/projects
POST   /v1/projects/{id}/uploads:start
POST   /v1/projects/{id}/uploads:complete
GET    /v1/projects/{id}
GET    /v1/projects/{id}/events
GET    /v1/projects/{id}/transcript
PATCH  /v1/projects/{id}/transcript/segments/{segmentId}
GET    /v1/projects/{id}/scripts
POST   /v1/projects/{id}/scripts/{version}:select
PATCH  /v1/projects/{id}/script-segments/{segmentId}
POST   /v1/projects/{id}/script-segments/{segmentId}:regenerate
GET    /v1/projects/{id}/storyboards/{version}
PATCH  /v1/projects/{id}/shots/{shotId}
POST   /v1/projects/{id}/shots/{shotId}:regenerate
POST   /v1/projects/{id}/shots/{shotId}:select-attempt
POST   /v1/projects/{id}/references/{referenceId}:approve
POST   /v1/projects/{id}/render
POST   /v1/projects/{id}/review:approve
POST   /v1/projects/{id}/publish:youtube
```

Every mutation requires `If-Match: <row_version>` and an `Idempotency-Key`. Conflicts return the current version and a structured diff.

## Dependency invalidation

| User edit | Invalidate |
| --- | --- |
| Transcript text or speaker | Affected scene analysis, downstream plot/script if the changed evidence is selected |
| Script segment text | That narration segment, its forced alignment, covered shots, captions, render |
| Voice direction only | That audio segment, measured timing, retiming, affected shots only if new duration exceeds their valid envelope, captions, render |
| Character identity version | Unlocked keyframes/videos containing that character, shot QA, render |
| Shot prompt | That shot keyframe/video/QA, render |
| Selected shot attempt | Cross-shot continuity QA and render |
| Caption wording | Caption assets and render only |
| Thumbnail/title | Publication metadata only |

The UI shows the invalidation set before committing a broad edit.

# 21. Security and Secrets

## Identity and authorization

- Authenticate users with Microsoft Entra ID through OIDC.
- Authorize every project access by tenant, project membership, and role: Viewer, Editor, Approver, Publisher, or Administrator.
- Use separate managed identities for API, workflow workers, render jobs, and publisher workers.
- Keep provider adapters in workers that need their secrets. The web app and most API routes never receive provider keys.

## Secret storage

- Store OpenAI, Runway, ElevenLabs, Google, and YouTube client secrets in Azure Key Vault.
- Reference Key Vault from Container Apps using managed identity. Do not copy secrets into images, build arguments, repository variables, logs, or workflow payloads.
- Rotate third-party keys, version them, and support overlapping active versions during rotation.
- Store YouTube refresh tokens encrypted with a Key Vault-backed key and restrict decryption to publisher workers.

Azure Container Apps can reference Key Vault secrets through managed identity, and secret version omission allows automatic refresh behavior. [Microsoft Key Vault references for Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets)

## Media protection

- Upload directly to Blob Storage using short-lived, write-only scoped URLs.
- Download through short-lived user-delegation SAS tokens restricted to one object and HTTPS.
- Encrypt at rest with platform keys by default, customer-managed keys if required.
- Use private endpoints for Blob, PostgreSQL, Redis, Service Bus, and Key Vault.
- Isolate each render into a new container and random per-job directory. Delete ephemeral bytes after upload and verification.
- Validate declared MIME type against probe results, cap dimensions/duration/size, and reject unexpected archive or executable formats.
- Use content-addressed names instead of user filenames for internal paths.
- Define automatic retention separately for source, failed attempts, approved references, and final renders.

## Application controls

- Parameterized SQL only.
- Strict Pydantic request bodies with unknown fields rejected for privileged mutations.
- CSP, same-site secure cookies, CSRF protection where cookies are used, and narrow CORS.
- Signed webhook verification plus replay timestamp and event-ID deduplication.
- Egress allowlists from provider workers where operationally practical.
- Immutable audit events for approval, provider change, asset selection, publication, and secret-access failures.

# 22. Testing Strategy

## Unit tests

- Timing allocation, discrete-duration rounding, shot splitting, and 40 ms drift invariant.
- Transcript chunk overlap reconciliation.
- Character-state interval folding and persistent state behavior.
- Input hashing and canonical JSON serialization.
- Provider routing and cost reservation.
- FFmpeg command construction with argument arrays.
- Caption line grouping and safe-zone layout.
- QA score recomputation and hard-failure rules.
- Dependency invalidation after every editable entity type.

## Contract tests

- Generate JSON Schema from Pydantic and TypeScript types from the checked-in schema.
- Validate every agent/provider fixture against its contract.
- Reject unknown enum values, missing source references, dangling IDs, invalid time ranges, and score totals.
- Maintain backward-compatibility tests for one prior contract version and explicit migration functions.

## Agent evaluations

Use a fixed evidence package and score:

- Episode beat recall and unsupported-claim rate.
- Character alias precision.
- Plot causal completeness.
- Mandatory beat coverage.
- Script word-budget compliance and plot fidelity.
- Joke mechanism diversity and human preference.
- Storyboard feasibility and exact timing.
- Visual QA precision/recall by failure code.

Model or prompt upgrades run the complete eval suite. Production promotion requires no regression on hard metrics and a documented review of subjective metrics.

## Integration tests

- Real PostgreSQL, Redis, Azurite, Temporal, and FFmpeg in CI.
- Fake provider adapters for success, delayed completion, 429, 500, malformed output, callback duplication, and timeout.
- Nightly tests against provider sandbox/test accounts for SDK and API drift.
- Terminate a worker after remote task submission, after output upload, and before DB commit. Verify exactly one paid task and one selected output.
- Render an MP4 and decode the complete result with FFmpeg.

## Deterministic pipeline tests

Record agent and provider outputs, then replay the workflow without external calls. The same contracts must produce identical input hashes, stage transitions, render manifest, caption timings, and FFmpeg arguments. Binary video hashes may vary by encoder build, so assert stream metadata, perceptual frame hashes, duration, and loudness tolerances.

## Golden episodes

Maintain at least five synthetic or controlled test inputs:

1. One speaker, simple chronology.
2. Rapid two-person dialogue and interruptions.
3. Four recurring characters with wardrobe changes.
4. Dark scenes, fades, subtitles, and flash cuts.
5. Nonlinear timeline with a reveal that depends on an earlier prop.

Each golden case includes approved scene boundaries, transcript ranges, characters, state events, plot beats, compressed plan, script rubric, storyboard timing, known bad visual samples, and final technical thresholds.

## Video QA tests

Create controlled defects: wrong identity, duplicated person, changed jacket, missing scar, extra finger, warped face, random sign text, wrong room, reversed screen direction, frozen motion, black frames, excessive flicker, and style shift. Track precision, recall, and false approval rate per defect. A hard-defect false approval blocks release.

## Load and chaos tests

- Start 20 projects and 1,300 shot workflows with fake providers.
- Inject PostgreSQL failover, Redis loss, worker restarts, blob timeouts, provider 429 storms, and duplicate callbacks.
- Verify workflows recover, Redis loss does not lose truth, budget caps remain enforced, and no duplicate provider submissions occur.

# 23. First Working Vertical Slice

## Exact target

```text
Upload one 10-minute MP4
  -> FFmpeg audio and 720p proxy
  -> OpenAI transcription
  -> scene detection plus 20 representative frames
  -> LLM creates a factual episode summary
  -> LLM writes a 150 to 180 word, 60-second funny recap
  -> TTS generates narration and exact duration is measured
  -> LLM creates a 10-shot storyboard against measured audio
  -> generate 10 keyframes
  -> animate 10 keyframes
  -> create captions
  -> FFmpeg assembles and mixes
  -> final 1080p MP4
```

## Build sequence

1. Implement T01 through T05 with project fields fixed to `target_duration_ms=60000`, `humor_intensity=0.7`, and one built-in style.
2. Implement `probe_video`, `extract_audio`, `make_proxy`, `detect_scenes`, and `extract_midpoint_frames` as idempotent activities.
3. Split speech FLAC below 25 MB, call `gpt-transcribe`, and merge chunks into `TranscriptSegment[]`.
4. Build one evidence contract containing transcript, detected scenes, and up to 20 frame asset IDs.
5. Call Episode Analyst with `EpisodeAnalysisLite` for characters, locations, ten scene summaries, and 8 to 12 plot beats.
6. Call a combined MVP Compressor/Comedy Writer. Require 150 to 180 words, every mandatory beat, and exactly ten `visualIntent` hints.
7. Generate TTS for each narration segment, measure every output, and concatenate a narration preview.
8. Call Storyboard Director with measured durations. The deterministic retimer creates exactly ten shots whose edit durations sum to narration duration.
9. Call `gpt-image-2` once per shot using the built-in style and representative source frame as context. Store every image as an asset.
10. Start ten shot child workflows using Runway `gen4_turbo`, bounded by the configured provider semaphore.
11. Normalize and trim each clip, create SRT from narration alignment, mix narration over a fixed quiet music bed, and render final 1080p H.264/AAC MP4.
12. Show the result, script, storyboard, prompts, and per-shot regenerate button in the React UI.

## Parent workflow pseudocode

```python
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

NETWORK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
    non_retryable_error_types=["ContractError", "InvalidMediaError"],
)

@workflow.defn
class VerticalSliceWorkflow:
    @workflow.run
    async def run(self, project_id: str) -> dict:
        media = await workflow.execute_activity(
            "prepare_source_media",
            project_id,
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=NETWORK_RETRY,
            task_queue="media-cpu",
        )

        transcript, scenes = await workflow.gather(
            workflow.execute_activity(
                "transcribe_audio", media.speech_asset_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=NETWORK_RETRY, task_queue="llm-analysis"),
            workflow.execute_activity(
                "detect_scenes_and_frames", media.proxy_asset_id,
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=NETWORK_RETRY, task_queue="media-cpu"),
        )

        analysis = await workflow.execute_activity(
            "analyze_episode_lite",
            {"project_id": project_id,
             "transcript_id": transcript.id,
             "scene_package_id": scenes.id},
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=NETWORK_RETRY,
            task_queue="llm-analysis",
        )

        script = await workflow.execute_activity(
            "write_mvp_recap",
            {"analysis_id": analysis.id, "target_words": 165,
             "humor_intensity": 0.7},
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=NETWORK_RETRY,
            task_queue="llm-analysis",
        )

        narration_parts = await workflow.gather(*[
            workflow.execute_activity(
                "synthesize_segment", segment.id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=NETWORK_RETRY, task_queue="voice")
            for segment in script.segments
        ])

        timing = await workflow.execute_activity(
            "measure_and_align_narration", [a.asset_id for a in narration_parts],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=NETWORK_RETRY, task_queue="media-cpu")

        storyboard = await workflow.execute_activity(
            "create_ten_shot_storyboard",
            {"analysis_id": analysis.id, "script_id": script.id,
             "timing_id": timing.id},
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=NETWORK_RETRY, task_queue="llm-analysis")

        shot_results = await workflow.gather(*[
            workflow.execute_child_workflow(
                VerticalSliceShotWorkflow.run,
                {"project_id": project_id, "shot_id": shot.id},
                id=f"{project_id}/shot/{shot.id}/{shot.input_hash}",
                task_queue="control")
            for shot in storyboard.shots
        ])

        render = await workflow.execute_activity(
            "render_vertical_slice",
            {"project_id": project_id,
             "storyboard_id": storyboard.id,
             "shot_asset_ids": [s.video_asset_id for s in shot_results],
             "timing_id": timing.id},
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
            task_queue="render")

        await workflow.execute_activity(
            "project_complete", {"project_id": project_id,
                                 "render_asset_id": render.asset_id},
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=NETWORK_RETRY, task_queue="control")
        return {"project_id": project_id, "final_asset_id": render.asset_id}
```

## Shot workflow pseudocode

```python
@workflow.defn
class VerticalSliceShotWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        image = await workflow.execute_activity(
            "generate_keyframe", request["shot_id"],
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=NETWORK_RETRY, task_queue="image-gen")

        video = await workflow.execute_activity(
            "animate_keyframe",
            {"shot_id": request["shot_id"], "image_asset_id": image.asset_id},
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=1),
            retry_policy=NETWORK_RETRY, task_queue="video-gen")

        normalized = await workflow.execute_activity(
            "normalize_shot_video", video.asset_id,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
            task_queue="media-cpu")
        return {"shot_id": request["shot_id"],
                "video_asset_id": normalized.asset_id}
```

## Vertical-slice definition of done

- A new developer can run the stack from a clean checkout using the commands in section 17.
- Uploading the golden 10-minute MP4 starts one visible Temporal workflow.
- The output is 1920x1080 H.264 with AAC audio and SRT captions, 55 to 70 seconds long.
- The ten shot rows retain prompts, models, task IDs, hashes, costs, source refs, and selected asset IDs.
- Terminating the video worker after five submissions produces no duplicate paid submissions when it restarts.
- Regenerating shot 6 creates a new attempt, re-runs only normalization and final render downstream, and leaves shots 1 to 5 and 7 to 10 untouched.
- The final render passes full decode, A/V drift, black-frame, loudness, and duration verification.
This vertical slice proves the durable architecture, narration-first timing, provider abstraction, shot-local restartability, provenance, and deterministic render path before investing in the harder continuity and QA layers.
