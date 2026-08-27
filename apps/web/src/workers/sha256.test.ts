import { describe, expect, it } from "vitest";

import { Sha256Stream, hashBlob } from "./sha256";

const EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

describe("streaming SHA-256", () => {
  it("matches the known digest of an empty input", () => {
    expect(new Sha256Stream().digest()).toBe(EMPTY);
  });

  it("matches the known digest of 'abc'", () => {
    const stream = new Sha256Stream();
    stream.update(new TextEncoder().encode("abc"));
    expect(stream.digest()).toBe(ABC);
  });

  it("is independent of how the input is chunked", () => {
    const bytes = new TextEncoder().encode("x".repeat(1000));
    const whole = new Sha256Stream();
    whole.update(bytes);
    const split = new Sha256Stream();
    for (let offset = 0; offset < bytes.length; offset += 7) {
      split.update(bytes.subarray(offset, Math.min(offset + 7, bytes.length)));
    }
    expect(split.digest()).toBe(whole.digest());
  });

  it("hashes a blob in bounded slices and reports progress", async () => {
    const content = "y".repeat(5000);
    const blob = new Blob([content]);
    const progress: number[] = [];
    const digest = await hashBlob(blob, 1024, (bytesHashed) => progress.push(bytesHashed));

    const expected = new Sha256Stream();
    expected.update(new TextEncoder().encode(content));
    expect(digest).toBe(expected.digest());
    // Progress advanced monotonically and never exceeded the file size.
    expect(progress[progress.length - 1]).toBe(5000);
    expect([...progress].sort((a, b) => a - b)).toEqual(progress);
    expect(progress.length).toBeGreaterThan(1);
  });
});
