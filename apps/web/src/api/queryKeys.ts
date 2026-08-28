/**
 * Centralised query keys.
 *
 * Keys are hierarchical so an event or mutation can invalidate exactly the
 * lineage it affects. Regenerating one shot invalidates that shot, never every
 * sibling shot query.
 */
export const queryKeys = {
  projects: () => ["projects"] as const,
  project: (projectId: string) => ["projects", projectId] as const,
  projectStatus: (projectId: string) => ["projects", projectId, "status"] as const,
  workflow: (projectId: string) => ["projects", projectId, "workflow"] as const,
  events: (projectId: string) => ["projects", projectId, "events"] as const,
  transcript: (projectId: string) => ["projects", projectId, "transcript"] as const,
  scripts: (projectId: string) => ["projects", projectId, "scripts"] as const,
  script: (projectId: string) => ["projects", projectId, "script"] as const,
  scriptVersion: (projectId: string, scriptId: string) =>
    ["projects", projectId, "scripts", scriptId] as const,
  storyboard: (projectId: string) => ["projects", projectId, "storyboard"] as const,
  shots: (projectId: string) => ["projects", projectId, "shots"] as const,
  shot: (projectId: string, shotId: string) =>
    ["projects", projectId, "shots", shotId] as const,
  shotStatus: (projectId: string, shotId: string) =>
    ["projects", projectId, "shots", shotId, "status"] as const,
  visualQa: (projectId: string) => ["projects", projectId, "visual-qa"] as const,
  shotVisualQa: (projectId: string, shotId: string) =>
    ["projects", projectId, "shots", shotId, "visual-qa"] as const,
  visualQaRun: (projectId: string, shotId: string, qaRunId: string) =>
    ["projects", projectId, "shots", shotId, "visual-qa", qaRunId] as const,
  visualQaEvidence: (projectId: string, shotId: string, qaRunId: string) =>
    ["projects", projectId, "shots", shotId, "visual-qa", qaRunId, "evidence"] as const,
  render: (projectId: string) => ["projects", projectId, "render"] as const,
  costs: (projectId: string) => ["projects", projectId, "costs"] as const,
  providerAttempts: (projectId: string) =>
    ["projects", projectId, "provider-attempts"] as const,
  failures: (projectId: string) => ["projects", projectId, "failures"] as const,
  downloadUrl: (assetId: string) => ["assets", assetId, "download-url"] as const,
} as const;

/** Signed download URLs are short-lived and must never be cached for long. */
export const DOWNLOAD_URL_STALE_MS = 30_000;
export const DOWNLOAD_URL_GC_MS = 60_000;
