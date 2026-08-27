import { readConfig, type WebConfig } from "./config";
import { NetworkError, toApiError, VidGenApiError } from "./errors";

export interface RequestOptions {
  readonly method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  readonly body?: unknown;
  /** Raw bytes for a resumable upload part; mutually exclusive with `body`. */
  readonly rawBody?: BodyInit;
  readonly query?: Record<string, string | number | boolean | undefined>;
  /** The row version last read. Required by every versioned mutation. */
  readonly ifMatch?: number;
  /** Required by every non-idempotent mutation; never auto-retried without it. */
  readonly idempotencyKey?: string;
  readonly signal?: AbortSignal;
  readonly contentType?: string;
}

export interface ApiResponse<T> {
  readonly data: T;
  /** The row version to echo back in the next `If-Match`. */
  readonly rowVersion: number | null;
}

/**
 * The single typed entry point to the VidGen API.
 *
 * Components never call `fetch` directly. Response bodies are not logged,
 * because they may contain transcript or script text; only method, path,
 * status and correlation ID are traced, and only in development.
 */
export class VidGenClient {
  private readonly config: WebConfig;
  /** Late-bound so a test double installed after construction is still used. */
  private readonly fetchImpl: typeof fetch | undefined;

  constructor(config: WebConfig = readConfig(), fetchImpl?: typeof fetch) {
    this.config = config;
    this.fetchImpl = fetchImpl;
  }

  private get transport(): typeof fetch {
    return this.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
    const url = this.buildUrl(path, options.query);
    const headers = new Headers({ Accept: "application/json" });
    // Development identity only. T18 does not implement production auth.
    headers.set("X-VidGen-User", this.config.devUser);
    if (options.ifMatch !== undefined) {
      headers.set("If-Match", String(options.ifMatch));
    }
    if (options.idempotencyKey !== undefined) {
      headers.set("Idempotency-Key", options.idempotencyKey);
    }

    let payload: BodyInit | undefined;
    if (options.rawBody !== undefined) {
      payload = options.rawBody;
      headers.set("Content-Type", options.contentType ?? "application/octet-stream");
    } else if (options.body !== undefined) {
      payload = JSON.stringify(options.body);
      headers.set("Content-Type", "application/json");
    }

    const method = options.method ?? "GET";
    let response: Response;
    try {
      const init: RequestInit = { method, headers, ...(payload === undefined ? {} : { body: payload }) };
      if (options.signal) {
        Object.assign(init, { signal: options.signal });
      }
      response = await this.transport(url, init);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") {
        throw cause;
      }
      this.trace(method, path, "network-error", null);
      throw new NetworkError();
    }

    const correlationId = response.headers.get("x-vidgen-correlation-id");
    this.trace(method, path, String(response.status), correlationId);

    if (response.status === 204) {
      return { data: undefined as T, rowVersion: parseEtag(response.headers.get("ETag")) };
    }

    const text = await response.text();
    const parsed: unknown = text === "" ? null : safeParse(text);
    if (!response.ok) {
      throw new VidGenApiError(response.status, toApiError(response.status, parsed));
    }
    return { data: parsed as T, rowVersion: parseEtag(response.headers.get("ETag")) };
  }

  async get<T>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}) {
    return this.request<T>(path, { ...options, method: "GET" });
  }

  async post<T>(path: string, options: Omit<RequestOptions, "method"> = {}) {
    return this.request<T>(path, { ...options, method: "POST" });
  }

  async patch<T>(path: string, options: Omit<RequestOptions, "method"> = {}) {
    return this.request<T>(path, { ...options, method: "PATCH" });
  }

  async put<T>(path: string, options: Omit<RequestOptions, "method"> = {}) {
    return this.request<T>(path, { ...options, method: "PUT" });
  }

  /** The absolute URL of a streaming endpoint, for `EventSource`-style reads. */
  streamUrl(path: string, query: Record<string, string | number> = {}): string {
    return this.buildUrl(path, query);
  }

  get devUser(): string {
    return this.config.devUser;
  }

  private buildUrl(
    path: string,
    query?: Record<string, string | number | boolean | undefined>,
  ): string {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined) {
        search.set(key, String(value));
      }
    }
    const suffix = search.toString();
    return `${this.config.apiBaseUrl}${path}${suffix === "" ? "" : `?${suffix}`}`;
  }

  private trace(
    method: string,
    path: string,
    status: string,
    correlationId: string | null,
  ): void {
    if (!this.config.isDevelopment) {
      return;
    }
    // Redacted by construction: no bodies, no signed URLs, no headers.
    console.debug(`[vidgen] ${method} ${redactPath(path)} -> ${status}`, { correlationId });
  }
}

/** Replace UUID path segments so project identifiers stay out of dev logs. */
function redactPath(path: string): string {
  return path.replace(
    /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi,
    "{id}",
  );
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function parseEtag(value: string | null): number | null {
  if (value === null) {
    return null;
  }
  const digits = value.replace(/^W\//, "").replace(/"/g, "");
  return /^\d+$/.test(digits) ? Number(digits) : null;
}

/** A stable client key so a duplicate submission cannot double-apply. */
export function newIdempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return `${prefix}-${random}`;
}

export const apiClient = new VidGenClient();
