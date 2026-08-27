import { act, renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { VidGenClient } from "../api/client";
import { server } from "../test/server";
import { apiError } from "../test/handlers";
import { useResumableUpload, validateFile } from "./useResumableUpload";

const BASE = "http://localhost";
const client = new VidGenClient({ apiBaseUrl: BASE, devUser: "owner-a", isDevelopment: false });
const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const UPLOAD_ID = "22222222-2222-4222-8222-222222222222";

function videoFile(bytes = 12, type = "video/mp4"): File {
  return new File([new Uint8Array(bytes)], "episode.mp4", { type });
}

function uploadHandlers(partSize: number, onPart?: (part: number) => void) {
  return [
    http.post(`${BASE}/api/v1/projects/:projectId/uploads`, () =>
      HttpResponse.json(
        {
          id: UPLOAD_ID,
          project_id: PROJECT_ID,
          filename: "episode.mp4",
          media_type: "video/mp4",
          expected_size: 12,
          expected_sha256: "a".repeat(64),
          part_size: partSize,
          status: "pending",
          completed_asset_id: null,
          error_code: null,
        },
        { status: 201 },
      ),
    ),
    http.put(`${BASE}/api/v1/uploads/:uploadId/parts/:partNumber`, ({ params }) => {
      const part = Number(params.partNumber);
      onPart?.(part);
      return HttpResponse.json({
        upload_id: UPLOAD_ID,
        part_number: part,
        byte_size: partSize,
        sha256: "b".repeat(64),
        duplicate: false,
      });
    }),
    http.post(`${BASE}/api/v1/uploads/:uploadId/complete`, () =>
      HttpResponse.json({
        upload_id: UPLOAD_ID,
        source_video_id: "33333333-3333-4333-8333-333333333333",
        asset_id: "44444444-4444-4444-8444-444444444444",
        sha256: "a".repeat(64),
        byte_size: 12,
        status: "completed",
      }),
    ),
  ];
}

const stubHash = () => Promise.resolve("a".repeat(64));

describe("validateFile", () => {
  it("rejects an unsupported media type", () => {
    expect(validateFile(videoFile(10, "image/png"))).toMatchObject({ ok: false });
  });

  it("rejects a file larger than the configured maximum", () => {
    expect(validateFile(videoFile(10), { maxBytes: 4 })).toMatchObject({ ok: false });
  });

  it("accepts a supported video within the limit", () => {
    expect(validateFile(videoFile(10))).toEqual({ ok: true });
  });
});

describe("useResumableUpload", () => {
  it("hashes, uploads every part, and completes", async () => {
    const parts: number[] = [];
    server.use(...uploadHandlers(4, (part) => parts.push(part)));
    const { result } = renderHook(() =>
      useResumableUpload({ client, partSize: 4, hashFile: stubHash }),
    );

    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });

    await waitFor(() => expect(result.current.state.phase).toBe("complete"));
    expect(parts.sort()).toEqual([0, 1, 2]);
    expect(result.current.state.bytesUploaded).toBe(12);
    expect(result.current.state.result?.status).toBe("completed");
  });

  it("refuses a file whose type is not supported before any request", async () => {
    const { result } = renderHook(() =>
      useResumableUpload({ client, partSize: 4, hashFile: stubHash }),
    );
    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12, "text/plain"));
    });
    expect(result.current.state.phase).toBe("failed");
    expect(result.current.state.errorCode).toBe("unsupported_file");
  });

  it("retries a transient part failure with bounded backoff", async () => {
    let attempts = 0;
    server.use(
      // Listed first so it takes precedence over the default part handler.
      http.put(`${BASE}/api/v1/uploads/:uploadId/parts/:partNumber`, () => {
        attempts += 1;
        if (attempts === 1) {
          return HttpResponse.json(apiError("internal_error", "Temporary", { retryable: true }), {
            status: 500,
          });
        }
        return HttpResponse.json({
          upload_id: UPLOAD_ID,
          part_number: 0,
          byte_size: 12,
          sha256: "b".repeat(64),
          duplicate: false,
        });
      }),
      ...uploadHandlers(12),
    );
    const { result } = renderHook(() =>
      useResumableUpload({ client, partSize: 12, hashFile: stubHash }),
    );
    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });
    await waitFor(() => expect(result.current.state.phase).toBe("complete"));
    expect(attempts).toBe(2);
  });

  it("treats a conflicting duplicate part as an actionable failure", async () => {
    let attempts = 0;
    server.use(
      // Listed first so it takes precedence over the default part handler.
      http.put(`${BASE}/api/v1/uploads/:uploadId/parts/:partNumber`, () => {
        attempts += 1;
        return HttpResponse.json(
          apiError("version_conflict", "A different body was already uploaded for this part.", {
            detail_code: "conflicting_part",
          }),
          { status: 409 },
        );
      }),
      ...uploadHandlers(12),
    );
    const { result } = renderHook(() =>
      useResumableUpload({ client, partSize: 12, hashFile: stubHash }),
    );
    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });
    expect(result.current.state.phase).toBe("failed");
    expect(result.current.state.errorCode).toBe("conflicting_part");
    // No retry: a conflict is never transient.
    expect(attempts).toBe(1);
  });

  it("cancels an in-flight upload", async () => {
    server.use(...uploadHandlers(4));
    const { result } = renderHook(() =>
      useResumableUpload({
        client,
        partSize: 4,
        hashFile: (_file, onProgress, signal) =>
          new Promise((_resolve, reject) => {
            onProgress(1, 12);
            signal.addEventListener("abort", () =>
              reject(new DOMException("cancelled", "AbortError")),
            );
          }),
      }),
    );
    const pending = act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });
    act(() => result.current.cancel());
    await pending;
    expect(result.current.state.phase).toBe("cancelled");
  });

  it("refuses to resume when the reselected file has a different fingerprint", async () => {
    server.use(...uploadHandlers(12));
    const hashes = ["a".repeat(64), "c".repeat(64)];
    let call = 0;
    const { result } = renderHook(() =>
      useResumableUpload({
        client,
        partSize: 12,
        hashFile: () => Promise.resolve(hashes[call++] ?? "a".repeat(64)),
      }),
    );
    // First attempt fails at finalization so the session is retained for resume.
    server.use(
      http.post(`${BASE}/api/v1/uploads/:uploadId/complete`, () =>
        HttpResponse.json(apiError("validation_failed", "Incomplete."), { status: 422 }),
      ),
    );
    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });
    expect(result.current.state.phase).toBe("failed");

    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });
    expect(result.current.state.errorCode).toBe("fingerprint_mismatch");
  });

  it("does not keep source bytes anywhere in its state", async () => {
    server.use(...uploadHandlers(12));
    const { result } = renderHook(() =>
      useResumableUpload({ client, partSize: 12, hashFile: stubHash }),
    );
    await act(async () => {
      await result.current.start(PROJECT_ID, videoFile(12));
    });
    await waitFor(() => expect(result.current.state.phase).toBe("complete"));
    const serialized = JSON.stringify(result.current.state);
    expect(serialized).not.toContain("Blob");
    expect(Object.values(result.current.state).some((value) => value instanceof Blob)).toBe(false);
    expect(vi.isMockFunction(globalThis.fetch)).toBe(false);
  });
});
