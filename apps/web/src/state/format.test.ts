import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatDurationSeconds,
  formatMicroseconds,
  formatMoney,
  formatStage,
  formatTimecode,
  formatTimestamp,
  shortId,
} from "./format";

describe("formatting", () => {
  it("renders exact decimal currency without float rounding", () => {
    expect(formatMoney("1.000000")).toBe("$1.00");
    expect(formatMoney("0.100000")).toBe("$0.10");
    expect(formatMoney("19.995000")).toBe("$19.995");
    expect(formatMoney(null)).toBe("—");
  });

  it("formats durations and timecodes consistently", () => {
    expect(formatDurationSeconds(75)).toBe("1:15");
    expect(formatDurationSeconds(3675)).toBe("1:01:15");
    expect(formatDurationSeconds(null)).toBe("—");
    expect(formatMicroseconds(3_000_000)).toBe("3.000s");
    expect(formatTimecode(63_500_000)).toBe("01:03.500");
  });

  it("formats timestamps in UTC", () => {
    expect(formatTimestamp("2026-08-01T09:05:00Z")).toContain("UTC");
    expect(formatTimestamp(null)).toBe("—");
  });

  it("labels pipeline stages in plain language", () => {
    expect(formatStage("shot_orchestration")).toBe("Shot orchestration");
    expect(formatStage(null)).toBe("Not started");
  });

  it("abbreviates identifiers only for technical details", () => {
    expect(shortId("11111111-1111-4111-8111-111111111111")).toBe("11111111…1111");
    expect(formatBytes(2048)).toBe("2.0 KB");
  });
});
