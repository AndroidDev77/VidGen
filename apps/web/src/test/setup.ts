import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { server } from "./server";

// jsdom does not implement these; the app reads them for the theme and for
// keyboard/pointer interactions inside Fluent UI.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

if (!("ResizeObserver" in window)) {
  Object.defineProperty(window, "ResizeObserver", {
    writable: true,
    value: class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  });
}

// jsdom has no EventSource. Without a stand-in, every page test would fall
// back to event polling and invalidate queries on a timer, which makes
// component assertions nondeterministic. SSE behaviour itself is covered
// directly in the useProjectEvents tests.
if (!("EventSource" in window)) {
  Object.defineProperty(window, "EventSource", {
    writable: true,
    value: class {
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: (() => void) | null = null;
      close(): void {}
    },
  });
}

if (!("scrollTo" in window)) {
  Object.defineProperty(window, "scrollTo", { writable: true, value: () => undefined });
}

// jsdom's Blob predates Blob.arrayBuffer; the upload path relies on it.
if (typeof Blob !== "undefined" && typeof Blob.prototype.arrayBuffer !== "function") {
  Object.defineProperty(Blob.prototype, "arrayBuffer", {
    writable: true,
    value(this: Blob): Promise<ArrayBuffer> {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as ArrayBuffer);
        reader.onerror = () => reject(reader.error ?? new Error("Blob read failed"));
        reader.readAsArrayBuffer(this);
      });
    },
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.clearAllTimers();
});
afterAll(() => server.close());
