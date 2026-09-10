import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { GenerationCostEstimate } from "@vidgen/contracts";

import type { GenerationSettingsInput } from "../api/projects";
import * as fixtures from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { GenerationSettingsPanel } from "./GenerationSettingsPanel";

const VALUE: GenerationSettingsInput = {
  generation_quality: "balanced",
  shot_pacing: "normal",
  premium_fallback_allowed: false,
  scene_detection_threshold: 0.3,
  warn_only_validation_codes: ["SCENE_SET_MISMATCH"],
  script_warn_only_validation_codes: ["UNKNOWN_SOURCE_REFERENCE"],
};

const LABEL = "Treat as warnings (not errors)";
const SCRIPT_LABEL = "Script: Treat as warnings (not errors)";

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

/**
 * The codes the panel last reported, sorted.
 *
 * Fluent's multiselect dropdown emits its selection in the order the options
 * were checked, which is not the order the API stores them in.
 */
function chosen(
  onChange: (next: GenerationSettingsInput) => void,
  field: "warn_only_validation_codes" | "script_warn_only_validation_codes" =
    "warn_only_validation_codes",
): string[] {
  const last = vi.mocked(onChange).mock.calls.at(-1);
  if (last === undefined) {
    throw new Error("the panel reported no change");
  }
  return [...last[0][field]].sort();
}

describe("GenerationSettingsPanel warn-only validation codes", () => {
  it("shows the codes the project currently treats as warnings", () => {
    renderPanel();
    expect(screen.getByRole("combobox", { name: LABEL })).toHaveValue("SCENE_SET_MISMATCH");
  });

  it("adds a code the owner selects without dropping the ones already chosen", async () => {
    const onChange = renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: LABEL }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /DUPLICATE_ID/ }));
    expect(chosen(onChange)).toEqual(["DUPLICATE_ID", "SCENE_SET_MISMATCH"]);
  });

  it("clears the last code when the owner deselects it, tolerating nothing", async () => {
    const onChange = renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: LABEL }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /SCENE_SET_MISMATCH/ }));
    expect(chosen(onChange)).toEqual([]);
  });
});

describe("GenerationSettingsPanel script warn-only validation codes", () => {
  it("shows the codes the project currently treats as warnings", () => {
    renderPanel();
    expect(screen.getByRole("combobox", { name: SCRIPT_LABEL })).toHaveValue(
      "UNKNOWN_SOURCE_REFERENCE",
    );
  });

  it("changes only the script codes, leaving the analysis codes alone", async () => {
    const onChange = renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: SCRIPT_LABEL }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /UNKNOWN_BEAT/ }));
    expect(chosen(onChange, "script_warn_only_validation_codes")).toEqual([
      "UNKNOWN_BEAT",
      "UNKNOWN_SOURCE_REFERENCE",
    ]);
    expect(chosen(onChange)).toEqual(["SCENE_SET_MISMATCH"]);
  });
});
