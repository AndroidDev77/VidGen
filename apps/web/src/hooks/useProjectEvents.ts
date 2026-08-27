import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import type { ProjectEventProjection } from "@vidgen/contracts";

import { apiClient, type VidGenClient } from "../api/client";
import { pollEvents } from "../api/events";
import { queryKeys } from "../api/queryKeys";

export type ConnectionState = "connecting" | "streaming" | "reconnecting" | "polling" | "closed";

export interface ProjectEventsState {
  readonly connection: ConnectionState;
  readonly lastEventId: number | null;
  readonly latest: ProjectEventProjection | null;
  readonly events: readonly ProjectEventProjection[];
  readonly reconnectAttempts: number;
}

/** After this many consecutive stream failures, fall back to status polling. */
export const MAX_SSE_ATTEMPTS = 3;
const BASE_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 15_000;
const POLL_INTERVAL_MS = 5_000;
/** A brief pause before reopening a stream the server closed cleanly. */
const CLEAN_RECONNECT_MS = 100;
const EVENT_BUFFER = 50;

export function backoffDelay(attempt: number): number {
  return Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
}

/** One parsed `text/event-stream` frame. */
export interface StreamFrame {
  readonly id: number | null;
  readonly data: string;
}

/** Split a Server-Sent Events buffer into complete frames plus a remainder. */
export function parseFrames(buffer: string): {
  frames: StreamFrame[];
  rest: string;
} {
  const frames: StreamFrame[] = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const block of parts) {
    let id: number | null = null;
    const data: string[] = [];
    for (const line of block.split("\n")) {
      // ":" alone is a heartbeat comment; it carries no payload.
      if (line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("id:")) {
        const parsed = Number(line.slice(3).trim());
        id = Number.isFinite(parsed) ? parsed : null;
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).trim());
      }
    }
    if (data.length > 0) {
      frames.push({ id, data: data.join("\n") });
    }
  }
  return { frames, rest };
}

export interface UseProjectEventsOptions {
  readonly client?: VidGenClient;
  readonly enabled?: boolean;
  /**
   * Injected in tests. Production streams through the API client so the
   * request can carry the development identity and `Last-Event-ID`, which
   * `EventSource` cannot do.
   */
  readonly openStream?: (
    lastEventId: number | undefined,
    signal: AbortSignal,
  ) => Promise<ReadableStreamDefaultReader<Uint8Array>>;
  readonly pollIntervalMs?: number;
}

/**
 * Subscribe to bounded project progress events.
 *
 * Events invalidate only the queries they affect, so an event about the render
 * never discards the storyboard cache. Repeated stream failures degrade to
 * status polling rather than leaving the dashboard silently stale.
 */
export function useProjectEvents(
  projectId: string,
  options: UseProjectEventsOptions = {},
): ProjectEventsState {
  const queryClient = useQueryClient();
  const client = options.client ?? apiClient;
  const enabled = options.enabled ?? true;
  const [state, setState] = useState<ProjectEventsState>({
    connection: "connecting",
    lastEventId: null,
    latest: null,
    events: [],
    reconnectAttempts: 0,
  });
  const seenRef = useRef<Set<number>>(new Set());
  const lastIdRef = useRef<number | null>(null);

  const ingest = useCallback(
    (event: ProjectEventProjection) => {
      if (seenRef.current.has(event.event_id)) {
        return;
      }
      seenRef.current.add(event.event_id);
      lastIdRef.current = event.event_id;
      setState((previous) => ({
        ...previous,
        lastEventId: event.event_id,
        latest: event,
        events: [...previous.events, event].slice(-EVENT_BUFFER),
      }));
      invalidateForEvent(queryClient, projectId, event);
    },
    [projectId, queryClient],
  );

  useEffect(() => {
    if (!enabled) {
      setState((previous) => ({ ...previous, connection: "closed" }));
      return;
    }
    let disposed = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let pollTimer: ReturnType<typeof setInterval> | undefined;
    let attempts = 0;
    const controller = new AbortController();

    const startPolling = () => {
      setState((previous) => ({ ...previous, connection: "polling" }));
      const tick = () => {
        void pollEvents(projectId, lastIdRef.current, client)
          .then(({ data }) => {
            if (!disposed) {
              data.items.forEach(ingest);
            }
          })
          .catch(() => undefined);
      };
      tick();
      pollTimer = setInterval(tick, options.pollIntervalMs ?? POLL_INTERVAL_MS);
    };

    const openStream =
      options.openStream ??
      ((lastEventId: number | undefined, signal: AbortSignal) =>
        client.openEventStream(`/api/v1/projects/${projectId}/events`, {
          lastEventId,
          signal,
        }));

    const fail = () => {
      if (disposed) {
        return;
      }
      attempts += 1;
      const exhausted = attempts >= MAX_SSE_ATTEMPTS;
      setState((previous) => ({
        ...previous,
        connection: exhausted ? "polling" : "reconnecting",
        reconnectAttempts: attempts,
      }));
      if (exhausted) {
        startPolling();
        return;
      }
      reconnectTimer = setTimeout(() => void connect(), backoffDelay(attempts));
    };

    const connect = async (): Promise<void> => {
      if (disposed) {
        return;
      }
      let reader: ReadableStreamDefaultReader<Uint8Array>;
      try {
        reader = await openStream(lastIdRef.current ?? undefined, controller.signal);
      } catch {
        fail();
        return;
      }
      if (disposed) {
        void reader.cancel();
        return;
      }
      attempts = 0;
      setState((previous) => ({ ...previous, connection: "streaming", reconnectAttempts: 0 }));
      const decoder = new TextDecoder();
      let buffer = "";
      // A stream the server closed cleanly is a reconnect, not a failure: it
      // must not count toward the polling-fallback budget.
      let closedCleanly = false;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) {
            closedCleanly = true;
            break;
          }
          if (disposed) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          const { frames, rest } = parseFrames(buffer);
          buffer = rest;
          for (const frame of frames) {
            try {
              ingest(JSON.parse(frame.data) as ProjectEventProjection);
            } catch {
              // A malformed frame is dropped; the next one still arrives.
            }
          }
        }
      } catch {
        // Fall through to the reconnect path below.
      }
      if (disposed) {
        return;
      }
      if (closedCleanly) {
        reconnectTimer = setTimeout(() => void connect(), CLEAN_RECONNECT_MS);
        return;
      }
      fail();
    };

    void connect();
    return () => {
      disposed = true;
      controller.abort();
      if (reconnectTimer !== undefined) {
        clearTimeout(reconnectTimer);
      }
      if (pollTimer !== undefined) {
        clearInterval(pollTimer);
      }
    };
  }, [client, enabled, ingest, options.openStream, options.pollIntervalMs, projectId]);

  return state;
}

/** Map one event onto exactly the queries it can affect. */
export function invalidateForEvent(
  queryClient: { invalidateQueries: (filters: { queryKey: readonly unknown[] }) => unknown },
  projectId: string,
  event: ProjectEventProjection,
): void {
  const invalidate = (queryKey: readonly unknown[]) => queryClient.invalidateQueries({ queryKey });
  invalidate(queryKeys.workflow(projectId));
  switch (event.event_type) {
    case "transcript_edited":
      invalidate(queryKeys.transcript(projectId));
      break;
    case "script_edited":
    case "script_selected":
      invalidate(queryKeys.script(projectId));
      invalidate(queryKeys.scripts(projectId));
      break;
    case "shot_regeneration_started":
    case "shot_retry_requested":
    case "shot_cancel_requested":
    case "shot_attempt_selected":
      invalidate(queryKeys.storyboard(projectId));
      break;
    case "render_started":
    case "render_marked_stale":
    case "render_approved":
      invalidate(queryKeys.render(projectId));
      break;
    default:
      break;
  }
  if (event.cost_summary_version !== null) {
    invalidate(queryKeys.costs(projectId));
  }
  if (event.failure_code !== null) {
    invalidate(queryKeys.failures(projectId));
  }
}
