import { describe, expect, it, vi } from "vitest";

import { VidGenClient, newIdempotencyKey } from "./client";
import { readConfig } from "./config";
import { NetworkError, VidGenApiError, classify } from "./errors";

const config = { apiBaseUrl: "http://localhost", devUser: "owner-a", isDevelopment: false };

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("VidGenClient", () => {
  it("sends the development identity, If-Match and Idempotency-Key headers", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve(jsonResponse({ ok: true }, { headers: { ETag: '"7"' } })),
    );
    const client = new VidGenClient(config, fetchImpl as unknown as typeof fetch);

    const response = await client.patch("/api/v1/thing", {
      body: { a: 1 },
      ifMatch: 7,
      idempotencyKey: "key-1",
    });

    const call = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    const init = call[1];
    const headers = init.headers as Headers;
    expect(headers.get("X-VidGen-User")).toBe("owner-a");
    expect(headers.get("If-Match")).toBe("7");
    expect(headers.get("Idempotency-Key")).toBe("key-1");
    expect(response.rowVersion).toBe(7);
  });

  it("throws a structured VidGenApiError for a failing response", async () => {
    const body = {
      schema_version: "1.0",
      code: "version_conflict",
      summary: "It changed.",
      retryable: false,
      current_version: 9,
      workflow_id: null,
      stage: null,
      fields: [],
      correlation_id: "trace-1",
      detail_code: null,
    };
    const client = new VidGenClient(
      config,
      (() => Promise.resolve(jsonResponse(body, { status: 409 }))) as unknown as typeof fetch,
    );

    await expect(client.get("/api/v1/thing")).rejects.toBeInstanceOf(VidGenApiError);
    await client.get("/api/v1/thing").catch((error: unknown) => {
      expect(error).toBeInstanceOf(VidGenApiError);
      const failure = error as VidGenApiError;
      expect(failure.code).toBe("version_conflict");
      expect(failure.currentVersion).toBe(9);
      expect(classify(failure)).toBe("conflict");
    });
  });

  it("maps a transport failure to a NetworkError", async () => {
    const client = new VidGenClient(
      config,
      (() => Promise.reject(new TypeError("offline"))) as unknown as typeof fetch,
    );
    await expect(client.get("/api/v1/thing")).rejects.toBeInstanceOf(NetworkError);
  });

  it("propagates an AbortSignal cancellation unchanged", async () => {
    const client = new VidGenClient(
      config,
      (() =>
        Promise.reject(new DOMException("aborted", "AbortError"))) as unknown as typeof fetch,
    );
    await expect(client.get("/api/v1/thing")).rejects.toMatchObject({ name: "AbortError" });
  });

  it("builds unique idempotency keys per submission", () => {
    expect(newIdempotencyKey("x")).not.toBe(newIdempotencyKey("x"));
  });
});

describe("readConfig", () => {
  it("treats an empty base URL as same-origin", () => {
    expect(readConfig({ DEV: false } as ImportMetaEnv).apiBaseUrl).toBe("");
  });

  it("rejects a base URL that is not absolute http(s)", () => {
    expect(() =>
      readConfig({ DEV: false, VITE_VIDGEN_API_BASE_URL: "ftp://x" } as ImportMetaEnv),
    ).toThrow(/absolute http/);
  });

  it("falls back to the local development identity", () => {
    expect(readConfig({ DEV: true } as ImportMetaEnv).devUser).toBe("local-user");
  });
});
