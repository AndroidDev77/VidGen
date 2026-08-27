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
const EVENT_BUFFER = 50;

export function backoffDelay(attempt: number): number {
  return Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
}

export interface UseProjectEventsOptions {
  readonly client?: VidGenClient;
  readonly enabled?: boolean;
  /** Injected in tests; production uses the browser's `EventSource`. */
  readonly eventSourceFactory?: (url: string) => EventSource;
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
    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let pollTimer: ReturnType<typeof setInterval> | undefined;
    let attempts = 0;

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

    const connect = () => {
      if (disposed) {
        return;
      }
      const factory =
        options.eventSourceFactory ?? ((url: string) => new EventSource(url, { withCredentials: false }));
      const url = client.streamUrl(`/api/v1/projects/${projectId}/events`, {
        ...(lastIdRef.current === null ? {} : { last_event_id: lastIdRef.current }),
      });
      try {
        source = factory(url);
      } catch {
        startPolling();
        return;
      }
      source.onopen = () => {
        attempts = 0;
        setState((previous) => ({ ...previous, connection: "streaming", reconnectAttempts: 0 }));
      };
      source.onmessage = (message: MessageEvent<string>) => {
        try {
          ingest(JSON.parse(message.data) as ProjectEventProjection);
        } catch {
          // A malformed frame is dropped; the next one still arrives.
        }
      };
      source.onerror = () => {
        source?.close();
        source = null;
        if (disposed) {
          return;
        }
        attempts += 1;
        setState((previous) => ({
          ...previous,
          connection: attempts >= MAX_SSE_ATTEMPTS ? "polling" : "reconnecting",
          reconnectAttempts: attempts,
        }));
        if (attempts >= MAX_SSE_ATTEMPTS) {
          startPolling();
          return;
        }
        reconnectTimer = setTimeout(connect, backoffDelay(attempts));
      };
    };

    connect();
    return () => {
      disposed = true;
      source?.close();
      if (reconnectTimer !== undefined) {
        clearTimeout(reconnectTimer);
      }
      if (pollTimer !== undefined) {
        clearInterval(pollTimer);
      }
    };
  }, [client, enabled, ingest, options.eventSourceFactory, options.pollIntervalMs, projectId]);

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
