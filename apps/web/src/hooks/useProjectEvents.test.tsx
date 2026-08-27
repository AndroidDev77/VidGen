import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
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
  invalidateForEvent,
  useProjectEvents,
} from "./useProjectEvents";

const BASE = "http://localhost";
const client = new VidGenClient({ apiBaseUrl: BASE, devUser: "owner-a", isDevelopment: false });

/** A controllable stand-in for the browser's EventSource. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }
}

/** Stable across renders so the subscription effect is not re-run. */
const fakeFactory = (url: string) => new FakeEventSource(url) as unknown as EventSource;

function wrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useProjectEvents", () => {
  it("reports streaming and ingests events", async () => {
    FakeEventSource.instances = [];
    const queryClient = new QueryClient();
    const { result } = renderHook(
      () =>
        useProjectEvents(PROJECT_ID, {
          client,
          eventSourceFactory: fakeFactory,
        }),
      { wrapper: wrapper(queryClient) },
    );

    const source = FakeEventSource.instances[0];
    expect(source).toBeDefined();
    act(() => source?.onopen?.());
    await waitFor(() => expect(result.current.connection).toBe("streaming"));

    act(() =>
      source?.onmessage?.(
        new MessageEvent("message", { data: JSON.stringify(projectEvent(1)) }),
      ),
    );
    await waitFor(() => expect(result.current.lastEventId).toBe(1));
    expect(result.current.events).toHaveLength(1);
  });

  it("deduplicates repeated event IDs", async () => {
    FakeEventSource.instances = [];
    const queryClient = new QueryClient();
    const { result } = renderHook(
      () =>
        useProjectEvents(PROJECT_ID, {
          client,
          eventSourceFactory: fakeFactory,
        }),
      { wrapper: wrapper(queryClient) },
    );
    const source = FakeEventSource.instances[0];
    const frame = new MessageEvent("message", { data: JSON.stringify(projectEvent(4)) });
    act(() => source?.onmessage?.(frame));
    act(() => source?.onmessage?.(frame));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
  });

  it("enters a reconnecting state and backs off", () => {
    vi.useFakeTimers();
    try {
      FakeEventSource.instances = [];
      const queryClient = new QueryClient();
      const { result } = renderHook(
        () =>
          useProjectEvents(PROJECT_ID, {
            client,
            eventSourceFactory: fakeFactory,
          }),
        { wrapper: wrapper(queryClient) },
      );
      act(() => FakeEventSource.instances[0]?.onerror?.());
      expect(result.current.connection).toBe("reconnecting");
      expect(result.current.reconnectAttempts).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("falls back to polling after repeated stream failures", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/events`, () =>
        HttpResponse.json({ items: [projectEvent(9)], last_event_id: 9 }),
      ),
    );
    FakeEventSource.instances = [];
    const queryClient = new QueryClient();
    const { result } = renderHook(
      () =>
        useProjectEvents(PROJECT_ID, {
          client,
          pollIntervalMs: 10_000,
          eventSourceFactory: fakeFactory,
        }),
      { wrapper: wrapper(queryClient) },
    );
    for (let attempt = 0; attempt < MAX_SSE_ATTEMPTS; attempt += 1) {
      act(() => FakeEventSource.instances.at(-1)?.onerror?.());
    }
    await waitFor(() => expect(result.current.connection).toBe("polling"));
    await waitFor(() => expect(result.current.lastEventId).toBe(9));
  });

  it("backs off with a bounded exponential delay", () => {
    expect(backoffDelay(1)).toBe(1000);
    expect(backoffDelay(2)).toBe(2000);
    expect(backoffDelay(20)).toBe(15_000);
  });
});

describe("invalidateForEvent", () => {
  it("invalidates only the queries an event affects", () => {
    const invalidateQueries = vi.fn();
    invalidateForEvent({ invalidateQueries }, PROJECT_ID, projectEvent(1, {
      event_type: "shot_regeneration_started",
    }));
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
    invalidateForEvent({ invalidateQueries }, PROJECT_ID, projectEvent(2, {
      event_type: "script_edited",
    }));
    const keys = invalidateQueries.mock.calls.map(([filters]) =>
      JSON.stringify((filters as { queryKey: unknown[] }).queryKey),
    );
    expect(keys).toContain(JSON.stringify(queryKeys.script(PROJECT_ID)));
    expect(keys).toContain(JSON.stringify(queryKeys.scripts(PROJECT_ID)));
    expect(keys).not.toContain(JSON.stringify(queryKeys.storyboard(PROJECT_ID)));
  });
});
