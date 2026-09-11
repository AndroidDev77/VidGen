import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { GenerationCostEstimate } from "@vidgen/contracts";

import type { GenerationSettingsInput } from "../api/projects";
import * as fixtures from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import {
  GenerationSettingsPanel,
  NARRATION_WARN_ONLY_QUALITY_CODES,
  SCRIPT_WARN_ONLY_VALIDATION_CODES,
  STORYBOARD_WARN_ONLY_VALIDATION_CODES,
  VISUAL_QA_WARN_ONLY_CODES,
  WARN_ONLY_VALIDATION_CODES,
} from "./GenerationSettingsPanel";
import type { WARN_ONLY_GROUPS } from "./GenerationSettingsPanel";

type CodeField = (typeof WARN_ONLY_GROUPS)[number]["field"];

const VALUE: GenerationSettingsInput = {
  generation_quality: "balanced",
  shot_pacing: "normal",
  premium_fallback_allowed: false,
  scene_detection_threshold: 0.3,
  warn_only_validation_codes: ["SCENE_SET_MISMATCH"],
  script_warn_only_validation_codes: ["UNKNOWN_SOURCE_REFERENCE"],
  storyboard_warn_only_validation_codes: ["continuity_contradiction"],
  narration_warn_only_quality_codes: ["alignment_coverage"],
  visual_qa_warn_only_codes: ["AMBIGUOUS_VISUAL_EVIDENCE"],
};

const SECTION = "Treat errors as warnings";
const ANALYSIS = "Episode analysis: treat as warnings";
const SCRIPT = "Script: treat as warnings";
const STORYBOARD = "Storyboard: treat as warnings";
const NARRATION = "Narration: treat as warnings";
const VISUAL_QA = "Visual QA: treat as warnings";

function renderPanel(): (next: GenerationSettingsInput) => void {
  const onChange = vi.fn<(next: GenerationSettingsInput) => void>();
  renderWithProviders(
    <GenerationSettingsPanel
      value={VALUE}
      onChange={onChange}
      targetDurationSeconds={300}
      estimate={fixtures.generationEstimate as GenerationCostEstimate}
    />,
  );
  return onChange;
}

/** Render the panel and open the collapsed warn-only section. */
async function renderExpanded(): Promise<(next: GenerationSettingsInput) => void> {
  const onChange = renderPanel();
  await userEvent.setup().click(screen.getByRole("button", { name: SECTION }));
  return onChange;
}

/**
 * The codes the panel last reported for one stage, sorted.
 *
 * Fluent's multiselect dropdown emits its selection in the order the options
 * were checked, which is not the order the API stores them in.
 */
function chosen(
  onChange: (next: GenerationSettingsInput) => void,
  field: CodeField = "warn_only_validation_codes",
): string[] {
  const last = vi.mocked(onChange).mock.calls.at(-1);
  if (last === undefined) {
    throw new Error("the panel reported no change");
  }
  return [...last[0][field]].sort();
}

describe("GenerationSettingsPanel warn-only section", () => {
  it("keeps the codes out of the way until the section is expanded", async () => {
    renderPanel();
    expect(screen.queryByRole("combobox", { name: ANALYSIS })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: SECTION }));
    expect(screen.getByRole("combobox", { name: ANALYSIS })).toBeInTheDocument();
  });

  it("offers every stage, narration and visual QA included", async () => {
    await renderExpanded();
    for (const name of [ANALYSIS, SCRIPT, STORYBOARD, NARRATION, VISUAL_QA]) {
      expect(screen.getByRole("combobox", { name })).toBeInTheDocument();
    }
  });

  it("shows the codes each stage currently treats as warnings", async () => {
    await renderExpanded();
    expect(screen.getByRole("combobox", { name: ANALYSIS })).toHaveValue("SCENE_SET_MISMATCH");
    expect(screen.getByRole("combobox", { name: NARRATION })).toHaveValue("alignment_coverage");
    expect(screen.getByRole("combobox", { name: VISUAL_QA })).toHaveValue(
      "AMBIGUOUS_VISUAL_EVIDENCE",
    );
  });
});

describe("GenerationSettingsPanel warn-only selection", () => {
  it("adds a code without dropping the ones already chosen", async () => {
    const onChange = await renderExpanded();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: ANALYSIS }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /DUPLICATE_ID/ }));
    expect(chosen(onChange)).toEqual(["DUPLICATE_ID", "SCENE_SET_MISMATCH"]);
  });

  it("clears the last code when the owner deselects it, tolerating nothing", async () => {
    const onChange = await renderExpanded();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: ANALYSIS }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /SCENE_SET_MISMATCH/ }));
    expect(chosen(onChange)).toEqual([]);
  });

  it("changes only the stage the owner touched", async () => {
    const onChange = await renderExpanded();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: NARRATION }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /speaking_rate/ }));
    expect(chosen(onChange, "narration_warn_only_quality_codes")).toEqual([
      "alignment_coverage",
      "speaking_rate",
    ]);
    expect(chosen(onChange)).toEqual(["SCENE_SET_MISMATCH"]);
    expect(chosen(onChange, "script_warn_only_validation_codes")).toEqual([
      "UNKNOWN_SOURCE_REFERENCE",
    ]);
    expect(chosen(onChange, "storyboard_warn_only_validation_codes")).toEqual([
      "continuity_contradiction",
    ]);
    expect(chosen(onChange, "visual_qa_warn_only_codes")).toEqual([
      "AMBIGUOUS_VISUAL_EVIDENCE",
    ]);
  });

  it("tolerates another visual QA repair code without dropping the one chosen", async () => {
    const onChange = await renderExpanded();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: VISUAL_QA }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /ANATOMY_BREAKAGE/ }));
    expect(chosen(onChange, "visual_qa_warn_only_codes")).toEqual([
      "AMBIGUOUS_VISUAL_EVIDENCE",
      "ANATOMY_BREAKAGE",
    ]);
    expect(chosen(onChange, "narration_warn_only_quality_codes")).toEqual(["alignment_coverage"]);
  });

  it("offers a recap-script code alongside the plot-compression codes", async () => {
    const onChange = await renderExpanded();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: SCRIPT }));
    await user.click(
      await screen.findByRole("menuitemcheckbox", { name: /MANDATORY_BEAT_NOT_COVERED/ }),
    );
    expect(chosen(onChange, "script_warn_only_validation_codes")).toEqual([
      "MANDATORY_BEAT_NOT_COVERED",
      "UNKNOWN_SOURCE_REFERENCE",
    ]);
  });
});

describe("GenerationSettingsPanel select all", () => {
  it.each([
    ["Episode analysis", "warn_only_validation_codes", WARN_ONLY_VALIDATION_CODES],
    ["Script", "script_warn_only_validation_codes", SCRIPT_WARN_ONLY_VALIDATION_CODES],
    ["Storyboard", "storyboard_warn_only_validation_codes", STORYBOARD_WARN_ONLY_VALIDATION_CODES],
    ["Narration", "narration_warn_only_quality_codes", NARRATION_WARN_ONLY_QUALITY_CODES],
    ["Visual QA", "visual_qa_warn_only_codes", VISUAL_QA_WARN_ONLY_CODES],
  ])("takes %s to every code at once", async (label, field, codes) => {
    const onChange = await renderExpanded();
    await userEvent
      .setup()
      .click(screen.getByRole("checkbox", { name: `Select all ${label} codes` }));
    expect(chosen(onChange, field as CodeField)).toEqual(
      codes.map((code) => code.value).sort(),
    );
  });

  it("reads as mixed while only some codes are chosen", async () => {
    await renderExpanded();
    expect(screen.getByRole("checkbox", { name: "Select all Narration codes" })).toBePartiallyChecked();
  });

  it("clears the stage when every code is already chosen", async () => {
    const onChange = vi.fn<(next: GenerationSettingsInput) => void>();
    renderWithProviders(
      <GenerationSettingsPanel
        value={{
          ...VALUE,
          narration_warn_only_quality_codes: NARRATION_WARN_ONLY_QUALITY_CODES.map(
            (code) => code.value,
          ),
        }}
        onChange={onChange}
        targetDurationSeconds={300}
        estimate={fixtures.generationEstimate as GenerationCostEstimate}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: SECTION }));
    const selectAll = screen.getByRole("checkbox", { name: "Select all Narration codes" });
    expect(selectAll).toBeChecked();
    // A full stage reads as a count, not an unreadable joined list.
    expect(screen.getByRole("combobox", { name: NARRATION })).toHaveValue(
      `All ${NARRATION_WARN_ONLY_QUALITY_CODES.length} codes`,
    );
    await user.click(selectAll);
    expect(chosen(onChange, "narration_warn_only_quality_codes")).toEqual([]);
  });
});

describe("GenerationSettingsPanel code lists", () => {
  /**
   * The panel's lists are written by hand so each code can carry a
   * description. These guards fail when the API starts offering a code the
   * panel does not, which would otherwise be invisible: the owner simply
   * could not choose it.
   */
  it.each([
    [
      "episode analysis",
      WARN_ONLY_VALIDATION_CODES,
      fixtures.generationSettings.available_warn_only_validation_codes,
    ],
    [
      "script",
      SCRIPT_WARN_ONLY_VALIDATION_CODES,
      fixtures.generationSettings.available_script_warn_only_validation_codes,
    ],
    [
      "storyboard",
      STORYBOARD_WARN_ONLY_VALIDATION_CODES,
      fixtures.generationSettings.available_storyboard_warn_only_validation_codes,
    ],
    [
      "narration",
      NARRATION_WARN_ONLY_QUALITY_CODES,
      fixtures.generationSettings.available_narration_warn_only_quality_codes,
    ],
    [
      "visual QA",
      VISUAL_QA_WARN_ONLY_CODES,
      fixtures.generationSettings.available_visual_qa_warn_only_codes,
    ],
  ])("offers every %s code the API accepts", (_stage, offered, available) => {
    expect([...offered.map((code) => code.value)].sort()).toEqual([...available].sort());
  });

  it("describes every code it offers", () => {
    for (const group of [
      WARN_ONLY_VALIDATION_CODES,
      SCRIPT_WARN_ONLY_VALIDATION_CODES,
      STORYBOARD_WARN_ONLY_VALIDATION_CODES,
      NARRATION_WARN_ONLY_QUALITY_CODES,
      VISUAL_QA_WARN_ONLY_CODES,
    ]) {
      for (const code of group) {
        expect(code.description.length).toBeGreaterThan(0);
      }
    }
  });
});
