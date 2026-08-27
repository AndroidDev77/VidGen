import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { VidGenClient } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import { PROJECT_ID, projectEvent } from "../test/fixtures";
import { server } from "../test/server";
import {
  MAX_SSE_ATTEMPTS,
  backoffDelay,
  cleanReconnectDelay,
  invalidateForEvent,
  parseFrames,
  useProjectEvents,
} from "./useProjectEvents";

const BASE = "http://localhost";
const client = new VidGenClient({ apiBaseUrl: BASE, devUser: "owner-a", isDevelopment: false });
const encoder = new TextEncoder();

/** A reader over a fixed set of `text/event-stream` chunks. */
function readerOf(chunks: readonly string[]): ReadableStreamDefaultReader<Uint8Array> {
  let index = 0;
  return {
    read: () =>
      Promise.resolve(
        index < chunks.length
          ? { done: false as const, value: encoder.encode(chunks[index++]) }
          : { done: true as const, value: undefined },
      ),
    cancel: () => Promise.resolve(),
    releaseLock: () => undefined,
    closed: Promise.resolve(undefined),
  } as unknown as ReadableStreamDefaultReader<Uint8Array>;
}

function sseFrame(eventId: number): string {
  const event = projectEvent(eventId);
  return `id: ${eventId}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`;
}

function wrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("parseFrames", () => {
  it("splits complete frames and keeps the remainder", () => {
    const { frames, rest } = parseFrames(`${sseFrame(1)}id: 2\ndata: {"partial"`);
    expect(frames).toHaveLength(1);
    expect(frames[0]?.id).toBe(1);
    expect(rest).toBe('id: 2\ndata: {"partial"');
  });

  it("ignores heartbeat comments", () => {
    const { frames } = parseFrames(": heartbeat\n\n");
    expect(frames).toEqual([]);
  });
});

describe("useProjectEvents", () => {
  it("streams events and reports the streaming state", async () => {
    const queryClient = new QueryClient();
    const openStream = vi.fn(() => Promise.resolve(readerOf([sseFrame(1), sseFrame(2)])));
    const { result } = renderHook(
      () => useProjectEvents(PROJECT_ID, { client, openStream }),
      { wrapper: wrapper(queryClient) },
    );
    await waitFor(() => expect(result.current.lastEventId).toBe(2));
    expect(result.current.events).toHaveLength(2);
  });

  it("resumes from the last event ID it received", async () => {
    const queryClient = new QueryClient();
    const seen: (number | undefined)[] = [];
    const openStream = vi.fn((lastEventId: number | undefined) => {
      seen.push(lastEventId);
      return Promise.resolve(readerOf(seen.length === 1 ? [sseFrame(4)] : []));
    });
    renderHook(() => useProjectEvents(PROJECT_ID, { client, openStream }), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(seen.length).toBeGreaterThan(1));
    expect(seen[0]).toBeUndefined();
    expect(seen[1]).toBe(4);
  });

  it("deduplicates repeated event IDs", async () => {
    const queryClient = new QueryClient();
    const openStream = vi.fn(() => Promise.resolve(readerOf([sseFrame(4), sseFrame(4)])));
    const { result } = renderHook(
      () => useProjectEvents(PROJECT_ID, { client, openStream }),
      { wrapper: wrapper(queryClient) },
    );
    await waitFor(() => expect(result.current.lastEventId).toBe(4));
    expect(result.current.events).toHaveLength(1);
  });

  it("falls back to polling after repeated stream failures", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/events`, () =>
        HttpResponse.json({ items: [projectEvent(9)], last_event_id: 9 }),
      ),
    );
    const queryClient = new QueryClient();
    const openStream = vi.fn(() => Promise.reject(new Error("stream unavailable")));
    const { result } = renderHook(
      () => useProjectEvents(PROJECT_ID, { client, openStream, pollIntervalMs: 10_000 }),
      { wrapper: wrapper(queryClient) },
    );
    await waitFor(() => expect(result.current.connection).toBe("polling"), { timeout: 5000 });
    expect(openStream.mock.calls.length).toBeGreaterThanOrEqual(MAX_SSE_ATTEMPTS);
    await waitFor(() => expect(result.current.lastEventId).toBe(9));
  });

  it("backs off with a bounded exponential delay", () => {
    expect(backoffDelay(1)).toBe(1000);
    expect(backoffDelay(2)).toBe(2000);
    expect(backoffDelay(20)).toBe(15_000);
  });

  it("backs off when a stream keeps closing without delivering anything", () => {
    // The first reopen is prompt, because a clean close is normal. Repeated
    // empty closes are not, and must not become a reconnect loop.
    expect(cleanReconnectDelay(1)).toBe(100);
    expect(cleanReconnectDelay(2)).toBe(500);
    expect(cleanReconnectDelay(3)).toBe(1000);
    expect(cleanReconnectDelay(30)).toBe(15_000);
  });

  it("holds the connection state steady across empty reconnects", async () => {
    const queryClient = new QueryClient();
    // Every stream closes immediately, exactly like a stub endpoint returning
    // an empty body. Reopening it must not produce a fresh state object, or
    // every subscribed page would re-render on a timer and replace its DOM
    // nodes under the user.
    const openStream = vi.fn(() => Promise.resolve(readerOf([])));
    const { result } = renderHook(() => useProjectEvents(PROJECT_ID, { client, openStream }), {
      wrapper: wrapper(queryClient),
    });

    await waitFor(() => expect(result.current.connection).toBe("streaming"));
    const settled = result.current;
    await waitFor(() => expect(openStream.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(result.current).toBe(settled);
  });
});

describe("invalidateForEvent", () => {
  it("invalidates only the queries an event affects", () => {
    const invalidateQueries = vi.fn();
    invalidateForEvent(
      { invalidateQueries },
      PROJECT_ID,
      projectEvent(1, { event_type: "shot_regeneration_started" }),
    );
    const keys = invalidateQueries.mock.calls.map(([filters]) =>
      JSON.stringify((filters as { queryKey: unknown[] }).queryKey),
    );
    expect(keys).toContain(JSON.stringify(queryKeys.storyboard(PROJECT_ID)));
    expect(keys).toContain(JSON.stringify(queryKeys.workflow(PROJECT_ID)));
    // A shot event never discards the transcript or script caches.
    expect(keys).not.toContain(JSON.stringify(queryKeys.transcript(PROJECT_ID)));
    expect(keys).not.toContain(JSON.stringify(queryKeys.script(PROJECT_ID)));
  });

  it("invalidates the script lineage for a script edit", () => {
    const invalidateQueries = vi.fn();
    invalidateForEvent(
      { invalidateQueries },
      PROJECT_ID,
      projectEvent(2, { event_type: "script_edited" }),
    );
    const keys = invalidateQueries.mock.calls.map(([filters]) =>
      JSON.stringify((filters as { queryKey: unknown[] }).queryKey),
    );
    expect(keys).toContain(JSON.stringify(queryKeys.script(PROJECT_ID)));
    expect(keys).toContain(JSON.stringify(queryKeys.scripts(PROJECT_ID)));
    expect(keys).not.toContain(JSON.stringify(queryKeys.storyboard(PROJECT_ID)));
  });
});
