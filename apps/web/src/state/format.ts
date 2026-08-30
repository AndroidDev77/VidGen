/** Consistent, locale-stable formatting for dates, durations and money. */

const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "—" : `${DATE_TIME.format(parsed)} UTC`;
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
    return "—";
  }
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

export function formatMicroseconds(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) {
    return "—";
  }
  return `${(micros / 1_000_000).toFixed(3)}s`;
}

export function formatTimecode(micros: number): string {
  const totalSeconds = micros / 1_000_000;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(3).padStart(6, "0")}`;
}

/**
 * Render an exact decimal currency amount.
 *
 * The API sends decimal strings; they are formatted as text and never parsed
 * into a binary float, so no cost value is ever rounded on the way to the eye.
 */
export function formatMoney(amount: string | null | undefined, currency = "USD"): string {
  if (amount === null || amount === undefined || amount === "") {
    return "—";
  }
  const [whole = "0", fraction = ""] = amount.split(".");
  const trimmed = fraction.replace(/0+$/, "");
  const cents = (trimmed + "00").slice(0, Math.max(2, trimmed.length));
  const symbol = currency === "USD" ? "$" : `${currency} `;
  return `${symbol}${whole}.${cents}`;
}

export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

const STAGE_LABELS: Record<string, string> = {
  upload: "Upload",
  media_processing: "Media processing",
  transcript_acquisition: "Transcript",
  evidence: "Evidence",
  episode_analysis: "Episode analysis",
  script_generation: "Script",
  narration: "Narration",
  storyboard: "Storyboard",
  keyframes: "Keyframes",
  animation: "Animation",
  shot_orchestration: "Shot orchestration",
  captions: "Captions",
  rendering: "Rendering",
  render_queued: "Render queued",
  render_claiming: "Render starting",
  render_preparing: "Preparing render",
  render_manifest_ready: "Render manifest ready",
  render_rendering: "Rendering",
  render_verifying: "Verifying render",
  render_persisting: "Storing render",
  render_complete: "Render complete",
  render_failed: "Render failed",
  render_cancelled: "Render cancelled",
  review: "Review",
};

export function formatStage(stage: string | null | undefined): string {
  if (!stage) {
    return "Not started";
  }
  return STAGE_LABELS[stage] ?? humanize(stage);
}

export function humanize(value: string): string {
  const spaced = value.replace(/[_:-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A short, readable label for a UUID shown only in technical details. */
export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}
