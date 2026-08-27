import type { ApiError, ApiErrorCode } from "@vidgen/contracts";

/** A structured API failure, carrying the contract the UI renders. */
export class VidGenApiError extends Error {
  readonly status: number;
  readonly error: ApiError;

  constructor(status: number, error: ApiError) {
    super(error.summary);
    this.name = "VidGenApiError";
    this.status = status;
    this.error = error;
  }

  get code(): ApiErrorCode {
    return this.error.code;
  }

  get retryable(): boolean {
    return this.error.retryable;
  }

  get currentVersion(): number | null {
    return this.error.current_version;
  }
}

/** A transport failure: the request never reached the API. */
export class NetworkError extends Error {
  constructor(message = "The VidGen API could not be reached.") {
    super(message);
    this.name = "NetworkError";
  }
}

const FALLBACK_SUMMARIES: Partial<Record<number, string>> = {
  404: "That item no longer exists, or it belongs to someone else.",
  409: "Someone else changed this first. Refresh to compare and try again.",
  412: "This change was based on an older version. Refresh and try again.",
  422: "The request could not be validated.",
  428: "This change needs the version you last loaded.",
  429: "Too many requests. Wait a moment and try again.",
  500: "The VidGen API hit an unexpected problem.",
};

export function toApiError(status: number, body: unknown): ApiError {
  if (isApiError(body)) {
    return body;
  }
  return {
    schema_version: "1.0",
    code: statusToCode(status),
    summary: FALLBACK_SUMMARIES[status] ?? "The request could not be completed.",
    retryable: status >= 500 || status === 429,
    current_version: null,
    workflow_id: null,
    stage: null,
    fields: [],
    correlation_id: null,
    detail_code: null,
  };
}

function statusToCode(status: number): ApiErrorCode {
  switch (status) {
    case 404:
      return "not_found";
    case 409:
    case 412:
      return "version_conflict";
    case 422:
      return "validation_failed";
    case 428:
      return "precondition_required";
    case 429:
      return "rate_limited";
    default:
      return "internal_error";
  }
}

function isApiError(body: unknown): body is ApiError {
  if (typeof body !== "object" || body === null) {
    return false;
  }
  const candidate = body as Record<string, unknown>;
  return typeof candidate.code === "string" && typeof candidate.summary === "string";
}

/** The user-facing category a failure belongs to, used to pick UI treatment. */
export type FailureKind =
  | "validation"
  | "conflict"
  | "ownership"
  | "retryable"
  | "lineage"
  | "budget"
  | "provider"
  | "verification"
  | "network"
  | "unknown";

export function classify(error: unknown): FailureKind {
  if (error instanceof NetworkError) {
    return "network";
  }
  if (!(error instanceof VidGenApiError)) {
    return "unknown";
  }
  switch (error.code) {
    case "validation_failed":
      return "validation";
    case "version_conflict":
    case "precondition_required":
    case "idempotency_key_mismatch":
    case "idempotency_key_required":
      return "conflict";
    case "not_found":
      return "ownership";
    case "shot_not_retryable":
    case "render_stale":
    case "attempt_not_eligible":
      return "lineage";
    case "budget_denied":
      return "budget";
    case "provider_unavailable":
    case "rate_limited":
      return "provider";
    case "render_not_verified":
      return "verification";
    default:
      return error.retryable ? "retryable" : "unknown";
  }
}
