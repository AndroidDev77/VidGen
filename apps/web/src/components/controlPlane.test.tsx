import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import * as fixtures from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { server } from "../test/server";
import { CommandsPanel } from "./CommandsPanel";
import { VoiceProfilePicker } from "./VoiceProfilePicker";

const BASE = "http://localhost";
const { PROJECT_ID } = fixtures;

describe("CommandsPanel", () => {
  it("shows every command with the status the backend actually recorded", async () => {
    renderWithProviders(<CommandsPanel projectId={PROJECT_ID} />);
    const list = await screen.findByRole("list", { name: "Control commands" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("Build continuity references")).toBeVisible();
    expect(
      within(rows[0]!).getByText("Waiting: reference approval required."),
    ).toBeVisible();
    expect(within(rows[0]!).getByText("Workflow vidgen-references-1")).toBeVisible();
  });

  it("never claims a workflow for a command that has not started one", async () => {
    renderWithProviders(<CommandsPanel projectId={PROJECT_ID} />);
    const list = await screen.findByRole("list", { name: "Control commands" });
    const failed = within(list).getAllByRole("listitem")[1]!;
    expect(within(failed).getByText("Regenerate a shot")).toBeVisible();
    expect(
      within(failed).getByText("The inputs this command was created against have changed."),
    ).toBeVisible();
    expect(within(failed).queryByText(/Workflow /)).toBeNull();
  });

  it("names the active generation run rather than inventing project state", async () => {
    renderWithProviders(<CommandsPanel projectId={PROJECT_ID} />);
    expect(await screen.findByText("Generation run 1, from upload.")).toBeVisible();
  });

  it("says so plainly when a project has issued no commands", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/commands`, () =>
        HttpResponse.json({ project_id: PROJECT_ID, items: [], generation_runs: [] }),
      ),
    );
    renderWithProviders(<CommandsPanel projectId={PROJECT_ID} />);
    expect(
      await screen.findByText("No command has been issued for this project yet."),
    ).toBeVisible();
    expect(screen.getByText("No generation run is active.")).toBeVisible();
  });
});

describe("VoiceProfilePicker", () => {
  it("offers the deployment's configured voices and marks the selected one", async () => {
    renderWithProviders(<VoiceProfilePicker projectId={PROJECT_ID} />);
    // The caption only renders once the selection has loaded, so waiting for it
    // is what makes the value assertion below meaningful rather than racy.
    expect(await screen.findByText(/Model fake-tts, en, wav\./)).toBeVisible();
    const dropdown = screen.getByRole("combobox", { name: "Narration voice" });
    expect(dropdown).toHaveValue("fake · vidgen-local-fake-voice");
    await userEvent.click(dropdown);
    expect(await screen.findByRole("option", { name: "fake · vidgen-local-fake-alt" })).toBeVisible();
  });

  it("selects a voice through the API and reports the change downstream", async () => {
    const selected: string[] = [];
    renderWithProviders(
      <VoiceProfilePicker
        projectId={PROJECT_ID}
        onSelected={(value) => selected.push(value)}
      />,
    );
    const dropdown = await screen.findByRole("combobox", { name: "Narration voice" });
    await userEvent.click(dropdown);
    await userEvent.click(await screen.findByRole("option", { name: "fake · vidgen-local-fake-alt" }));
    expect(selected).toEqual([fixtures.voiceProfiles.items[1]!.voice_profile_id]);
  });

  it("asks for a voice when the project has none, because the workflow needs one", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/voice-profiles`, () =>
        HttpResponse.json({
          project_id: PROJECT_ID,
          selected_voice_profile_id: null,
          items: fixtures.voiceProfiles.items.map((item) => ({ ...item, selected: false })),
        }),
      ),
    );
    renderWithProviders(<VoiceProfilePicker projectId={PROJECT_ID} />);
    expect(await screen.findByText("Select a narration voice to continue.")).toBeVisible();
  });

  it("never exposes a provider credential", async () => {
    renderWithProviders(<VoiceProfilePicker projectId={PROJECT_ID} />);
    await screen.findByRole("combobox", { name: "Narration voice" });
    expect(document.body.textContent).not.toMatch(/api[_ -]?key|secret|token/i);
  });
});
