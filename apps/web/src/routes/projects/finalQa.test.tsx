import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  FinalCompletionGateProjection,
  FinalEditorialRunDetailProjection,
  FinalFindingProjection,
} from "@vidgen/contracts";
import { describe, expect, it, vi } from "vitest";

import { FinalQAPanel } from "../../components/FinalQAPanel";
import { renderWithProviders } from "../../test/render";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "22222222-2222-4222-8222-222222222222";
const SHOT_ID = "33333333-3333-4333-8333-333333333333";

function finding(overrides: Partial<FinalFindingProjection>): FinalFindingProjection {
  return {
    finding_id: "44444444-4444-4444-8444-444444444444",
    category: "story_beat_coverage",
    severity: "review_required",
    blocking: false,
    confidence: 0.62,
    issue_code: "MISSING_STORY_BEAT",
    summary: "The reconciliation beat may not be shown.",
    start_us: 1_000_000,
    end_us: 2_000_000,
    shot_ids: [SHOT_ID],
    caption_cue_sequences: [],
    narration_segment_ids: [],
    evidence: [
      {
        evidence_id: "55555555-5555-4555-8555-555555555555",
        evidence_type: "contact_sheet_tile",
        start_us: 1_000_000,
        end_us: 1_000_000,
        frame_asset_id: null,
        sample_id: null,
        contact_sheet_asset_id: null,
        contact_sheet_position: 3,
        caption_cue_sequence: null,
        shot_id: SHOT_ID,
        measurement: null,
        threshold: null,
        explanation: "sampled frame",
      },
    ],
    expected_behavior: "the approved beat appears",
    observed_behavior: "no shot depicts it",
    remediation_target: "CORRECT_SCRIPT_UPSTREAM",
    provenance: "provider",
    resolved_by_review: false,
    ...overrides,
  };
}

function run(
  overrides: Partial<FinalEditorialRunDetailProjection> = {},
): FinalEditorialRunDetailProjection {
  return {
    final_editorial_run_id: RUN_ID,
    project_id: PROJECT_ID,
    final_render_asset_id: "66666666-6666-4666-8666-666666666666",
    render_manifest_asset_id: "77777777-7777-4777-8777-777777777777",
    render_identity: "a".repeat(64),
    final_qa_identity: "b".repeat(64),
    input_hash: "c".repeat(64),
    configuration_hash: "d".repeat(64),
    report_version: "final-editorial/1.0",
    status: "FINAL_QA_REVIEW_REQUIRED",
    phase: "COMPLETION_GATE",
    decision: "REVIEW",
    selected: true,
    blocking_finding_count: 0,
    review_finding_count: 1,
    warning_finding_count: 0,
    deterministic_failure_count: 0,
    remediation_targets: ["CORRECT_SCRIPT_UPSTREAM"],
    provider: "fake",
    model: "fake-final-editorial-1",
    adjudicated: true,
    cost_microusd: 54_000,
    report_asset_id: "88888888-8888-4888-8888-888888888888",
    contact_sheet_asset_id: null,
    error_code: null,
    row_version: 7,
    created_at: "2026-08-01T10:00:00Z",
    completed_at: "2026-08-01T10:05:00Z",
    measurements: {
      container_format: "mov,mp4,m4a",
      byte_size: 12_345_678,
      video_codec: "h264",
      audio_codec: "aac",
      width: 1920,
      height: 1080,
      pixel_format: "yuv420p",
      frame_rate: "24/1",
      container_duration_us: 2_500_000,
      video_duration_us: 2_500_000,
      audio_duration_us: 2_500_000,
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
        check_id: "99999999-9999-4999-8999-999999999999",
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
        category: "story_beat_coverage",
        applicable: true,
        score: 97,
        confidence: 0.94,
        blocking_finding_count: 0,
        review_finding_count: 1,
        warning_finding_count: 0,
        summary: "",
      },
    ],
    findings: [finding({})],
    remediation_routes: [
      {
        target: "CORRECT_SCRIPT_UPSTREAM",
        finding_ids: ["44444444-4444-4444-8444-444444444444"],
        shot_ids: [SHOT_ID],
        caption_cue_sequences: [],
        reason: "1 finding(s) routed to CORRECT_SCRIPT_UPSTREAM",
        requires_new_render: true,
      },
    ],
    adjudication_confidence: 0.62,
    adjudication_decided: false,
    gate_reasons: ["1 unresolved review finding(s)"],
    timeline_duration_us: 2_500_000,
    ...overrides,
  };
}

function gate(
  overrides: Partial<FinalCompletionGateProjection> = {},
): FinalCompletionGateProjection {
  return {
    project_id: PROJECT_ID,
    final_editorial_run_id: RUN_ID,
    final_render_asset_id: "66666666-6666-4666-8666-666666666666",
    decision: "REVIEW",
    allowed: false,
    reason: "final_qa_review_required",
    blocking_finding_count: 0,
    review_finding_count: 1,
    deterministic_failure_count: 0,
    gate_version: "final-gate/1.0",
    row_version: 7,
    ...overrides,
  };
}

function noop(): void {
  /* the panel under test does not need a real handler here */
}

describe("FinalQAPanel", () => {
  it("renders a timeline marker for every finding with its exact time range", () => {
    renderWithProviders(
      <FinalQAPanel
        run={run()}
        gate={gate()}
        busy={false}
        onRunFinalQa={noop}
        onCancel={noop}
        onResolveReview={noop}
        onRoute={noop}
      />,
    );
    const panel = screen.getByRole("region", { name: "Final editorial QA" });
    expect(
      within(panel).getByTestId("finding-marker-44444444-4444-4444-8444-444444444444"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("00:01.000–00:02.000")).toBeVisible();
    expect(within(panel).getByText("MISSING STORY BEAT")).toBeVisible();
    // The remediation target appears on the finding row and again on the route.
    expect(within(panel).getAllByText("CORRECT SCRIPT UPSTREAM").length).toBeGreaterThan(0);
  });

  it("says the project cannot complete while the gate withholds a pass", () => {
    renderWithProviders(
      <FinalQAPanel
        run={run()}
        gate={gate()}
        busy={false}
        onRunFinalQa={noop}
        onCancel={noop}
        onResolveReview={noop}
        onRoute={noop}
      />,
    );
    expect(screen.getByText("The project cannot complete yet")).toBeVisible();
  });

  it("offers a resolve action for an eligible semantic review finding", async () => {
    const onResolveReview = vi.fn();
    renderWithProviders(
      <FinalQAPanel
        run={run()}
        gate={gate()}
        busy={false}
        onRunFinalQa={noop}
        onCancel={noop}
        onResolveReview={onResolveReview}
        onRoute={noop}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(onResolveReview).toHaveBeenCalledWith("44444444-4444-4444-8444-444444444444");
  });

  it("never offers a way to pass a deterministic hard failure", () => {
    const blocked = run({
      status: "FINAL_QA_FAILED",
      decision: "FAIL",
      deterministic_failure_count: 2,
      blocking_finding_count: 2,
      review_finding_count: 0,
      findings: [
        finding({
          finding_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          severity: "blocking",
          blocking: true,
          issue_code: "VIDEO_DECODE_FAILURE",
          provenance: "deterministic",
          confidence: 1,
          remediation_target: "RERENDER_T17",
        }),
      ],
    });
    renderWithProviders(
      <FinalQAPanel
        run={blocked}
        gate={gate({
          decision: "FAIL",
          allowed: false,
          reason: "final_qa_failed",
          blocking_finding_count: 2,
          deterministic_failure_count: 2,
          review_finding_count: 0,
        })}
        busy={false}
        onRunFinalQa={noop}
        onCancel={noop}
        onResolveReview={noop}
        onRoute={noop}
      />,
    );
    expect(screen.getByText("Deterministic failures cannot be overridden")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Resolve" })).toBeNull();
  });

  it("prompts to run final QA when no report exists for the current render", async () => {
    const onRunFinalQa = vi.fn();
    renderWithProviders(
      <FinalQAPanel
        run={null}
        gate={gate({
          final_editorial_run_id: null,
          decision: null,
          reason: "final_qa_missing",
          review_finding_count: 0,
        })}
        busy={false}
        onRunFinalQa={onRunFinalQa}
        onCancel={noop}
        onResolveReview={noop}
        onRoute={noop}
      />,
    );
    expect(screen.getByText("Final QA has not run for this render")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Run final QA" }));
    expect(onRunFinalQa).toHaveBeenCalledTimes(1);
  });
});
