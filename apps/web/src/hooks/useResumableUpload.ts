import { useCallback, useMemo, useRef, useState } from "react";

import { apiClient, type VidGenClient } from "../api/client";
import { VidGenApiError } from "../api/errors";
import {
  completeUpload,
  initializeUpload,
  uploadPart,
  type CompletedUpload,
  type UploadSession,
} from "../api/uploads";
import { hashBlob, type HashResponse } from "../workers/sha256";

export const DEFAULT_PART_SIZE = 8 * 1024 * 1024;
export const HASH_CHUNK_SIZE = 4 * 1024 * 1024;
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024;
export const ALLOWED_MEDIA_TYPES = ["video/mp4", "video/quicktime"] as const;
const MAX_PART_RETRIES = 4;
const UPLOAD_CONCURRENCY = 3;

export type UploadPhase =
  | "idle"
  | "validating"
  | "hashing"
  | "uploading"
  | "finalizing"
  | "complete"
  | "failed"
  | "cancelled";

export interface UploadState {
  readonly phase: UploadPhase;
  readonly uploadId: string | null;
  readonly fingerprint: string | null;
  readonly fileName: string | null;
  readonly totalBytes: number;
  readonly bytesHashed: number;
  readonly bytesUploaded: number;
  readonly completedParts: readonly number[];
  readonly totalParts: number;
  readonly errorMessage: string | null;
  readonly errorCode: string | null;
  readonly result: CompletedUpload | null;
}

const INITIAL: UploadState = {
  phase: "idle",
  uploadId: null,
  fingerprint: null,
  fileName: null,
  totalBytes: 0,
  bytesHashed: 0,
  bytesUploaded: 0,
  completedParts: [],
  totalParts: 0,
  errorMessage: null,
  errorCode: null,
  result: null,
};

export interface ValidationOptions {
  readonly maxBytes?: number;
  readonly allowedMediaTypes?: readonly string[];
}

export function validateFile(
  file: File,
  options: ValidationOptions = {},
): { ok: true } | { ok: false; message: string } {
  const allowed = options.allowedMediaTypes ?? ALLOWED_MEDIA_TYPES;
  const maxBytes = options.maxBytes ?? MAX_UPLOAD_BYTES;
  if (!allowed.includes(file.type)) {
    return {
      ok: false,
      message: `${file.type || "That file type"} is not supported. Choose an MP4 or QuickTime video.`,
    };
  }
  if (file.size <= 0) {
    return { ok: false, message: "That file is empty." };
  }
  if (file.size > maxBytes) {
    return {
      ok: false,
      message: `That file is larger than the configured maximum of ${Math.floor(
        maxBytes / (1024 * 1024 * 1024),
      )} GB.`,
    };
  }
  return { ok: true };
}

/** A per-session fingerprint used to accept or refuse a resume. */
export function fileFingerprint(file: File, sha256: string): string {
  return `${file.name}:${file.size}:${sha256}`;
}

export interface UseResumableUploadOptions {
  readonly client?: VidGenClient;
  readonly partSize?: number;
  readonly hashChunkSize?: number;
  readonly validation?: ValidationOptions;
  /** Injected in tests; production uses the streaming worker implementation. */
  readonly hashFile?: (
    file: File,
    onProgress: (bytesHashed: number, totalBytes: number) => void,
    signal: AbortSignal,
  ) => Promise<string>;
  readonly onComplete?: (result: CompletedUpload) => void;
}

export interface ResumableUpload {
  readonly state: UploadState;
  start: (projectId: string, file: File) => Promise<void>;
  cancel: () => void;
  reset: () => void;
}

/**
 * Drive one resumable source-video upload.
 *
 * Source bytes are never stored in application state, local storage, logs or
 * the query cache: the browser `File` handle is sliced on demand, and the only
 * things retained are the upload ID, the completed part numbers, and the
 * fingerprint that lets a resume refuse a different file.
 */
export function useResumableUpload(options: UseResumableUploadOptions = {}): ResumableUpload {
  const client = options.client ?? apiClient;
  const partSize = options.partSize ?? DEFAULT_PART_SIZE;
  const [state, setState] = useState<UploadState>(INITIAL);
  const abortRef = useRef<AbortController | null>(null);
  // Kept across a resume within this session only; never persisted. The
  // completed parts live in the ref rather than in `state`, because `start`
  // resets the rendered state and a stale closure over it would re-upload
  // finished parts and double-count their bytes.
  const sessionRef = useRef<{
    upload: UploadSession;
    fingerprint: string;
    completedParts: Set<number>;
    bytesUploaded: number;
  } | null>(null);

  const hashFile = useMemo(() => {
    if (options.hashFile) {
      return options.hashFile;
    }
    const chunkSize = options.hashChunkSize ?? HASH_CHUNK_SIZE;
    return (
      file: File,
      onProgress: (bytesHashed: number, totalBytes: number) => void,
      signal: AbortSignal,
    ) => runWorkerHash(file, chunkSize, onProgress, signal);
  }, [options.hashFile, options.hashChunkSize]);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState((previous) =>
      previous.phase === "complete" ? previous : { ...previous, phase: "cancelled" },
    );
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    sessionRef.current = null;
    setState(INITIAL);
  }, []);

  const resumeState = () => sessionRef.current;

  const start = useCallback(
    async (projectId: string, file: File) => {
      const controller = new AbortController();
      abortRef.current = controller;
      const resumed = resumeState();
      setState({
        ...INITIAL,
        phase: "validating",
        fileName: file.name,
        totalBytes: file.size,
        uploadId: resumed?.upload.id ?? null,
        completedParts: [...(resumed?.completedParts ?? [])],
        bytesUploaded: resumed?.bytesUploaded ?? 0,
      });

      const validation = validateFile(file, options.validation ?? {});
      if (!validation.ok) {
        setState((previous) => ({
          ...previous,
          phase: "failed",
          errorCode: "unsupported_file",
          errorMessage: validation.message,
        }));
        return;
      }

      let sha256: string;
      try {
        setState((previous) => ({ ...previous, phase: "hashing" }));
        sha256 = await hashFile(
          file,
          (bytesHashed, totalBytes) =>
            setState((previous) => ({ ...previous, bytesHashed, totalBytes })),
          controller.signal,
        );
      } catch (error) {
        setState((previous) => ({
          ...previous,
          phase: controller.signal.aborted ? "cancelled" : "failed",
          errorCode: "hash_failed",
          errorMessage: messageFor(error, "The file could not be hashed."),
        }));
        return;
      }
      if (controller.signal.aborted) {
        setState((previous) => ({ ...previous, phase: "cancelled" }));
        return;
      }

      const fingerprint = fileFingerprint(file, sha256);
      if (resumed !== null && resumed.fingerprint !== fingerprint) {
        setState((previous) => ({
          ...previous,
          phase: "failed",
          errorCode: "fingerprint_mismatch",
          errorMessage:
            "That is a different file from the one this upload started with. " +
            "Reselect the original file, or start a new upload.",
        }));
        return;
      }

      let session: UploadSession;
      try {
        session =
          resumed?.upload ??
          (
            await initializeUpload(
              projectId,
              {
                filename: file.name,
                media_type: file.type,
                expected_size: file.size,
                expected_sha256: sha256,
                part_size: partSize,
              },
              client,
            )
          ).data;
      } catch (error) {
        setState((previous) => ({
          ...previous,
          phase: "failed",
          errorCode: codeFor(error),
          errorMessage: messageFor(error, "The upload could not be started."),
        }));
        return;
      }
      const completedParts = resumed?.completedParts ?? new Set<number>();
      sessionRef.current = {
        upload: session,
        fingerprint,
        completedParts,
        bytesUploaded: resumed?.bytesUploaded ?? 0,
      };

      const effectivePartSize = session.part_size;
      const totalParts = Math.max(1, Math.ceil(file.size / effectivePartSize));
      setState((previous) => ({
        ...previous,
        phase: "uploading",
        uploadId: session.id,
        fingerprint,
        totalParts,
      }));

      const queue = Array.from({ length: totalParts }, (_, index) => index).filter(
        (part) => !completedParts.has(part),
      );
      let uploadedBytes = sessionRef.current.bytesUploaded;
      let failure: { code: string; message: string } | null = null;

      const worker = async () => {
        for (;;) {
          const part = queue.shift();
          if (part === undefined || failure !== null || controller.signal.aborted) {
            return;
          }
          const begin = part * effectivePartSize;
          const slice = file.slice(begin, Math.min(begin + effectivePartSize, file.size));
          try {
            // Exactly one part is resident at a time: the whole file is never
            // read into memory.
            const bytes = new Uint8Array(await slice.arrayBuffer());
            await withRetries(
              () => uploadPart(session.id, part, bytes, client, controller.signal),
              controller.signal,
            );
            uploadedBytes += slice.size;
            completedParts.add(part);
            if (sessionRef.current !== null) {
              sessionRef.current.bytesUploaded = uploadedBytes;
            }
            setState((previous) => ({
              ...previous,
              bytesUploaded: uploadedBytes,
              completedParts: [...completedParts],
            }));
          } catch (error) {
            if (controller.signal.aborted) {
              return;
            }
            failure = {
              code: codeFor(error) ?? "part_upload_failed",
              message: messageFor(error, `Part ${part + 1} could not be uploaded.`),
            };
            return;
          }
        }
      };

      await Promise.all(
        Array.from({ length: Math.min(UPLOAD_CONCURRENCY, totalParts) }, () => worker()),
      );

      if (controller.signal.aborted) {
        setState((previous) => ({ ...previous, phase: "cancelled" }));
        return;
      }
      if (failure !== null) {
        const settled: { code: string; message: string } = failure;
        setState((previous) => ({
          ...previous,
          phase: "failed",
          errorCode: settled.code,
          errorMessage: settled.message,
        }));
        return;
      }

      try {
        setState((previous) => ({ ...previous, phase: "finalizing" }));
        const completed = await completeUpload(session.id, client);
        sessionRef.current = null;
        setState((previous) => ({ ...previous, phase: "complete", result: completed.data }));
        options.onComplete?.(completed.data);
      } catch (error) {
        setState((previous) => ({
          ...previous,
          phase: "failed",
          errorCode: codeFor(error),
          errorMessage: messageFor(error, "The upload could not be finalized."),
        }));
      }
    },
    [client, hashFile, options, partSize],
  );

  return { state, start, cancel, reset };
}

/** Retry a transient part failure with bounded exponential backoff. */
async function withRetries<T>(operation: () => Promise<T>, signal: AbortSignal): Promise<T> {
  let attempt = 0;
  for (;;) {
    try {
      return await operation();
    } catch (error) {
      // A conflicting duplicate part is actionable, never a retry.
      if (error instanceof VidGenApiError && !error.retryable && error.status !== 429) {
        throw error;
      }
      attempt += 1;
      if (attempt >= MAX_PART_RETRIES || signal.aborted) {
        throw error;
      }
      await delay(Math.min(2 ** attempt * 100, 4000), signal);
    }
  }
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

function codeFor(error: unknown): string | null {
  if (error instanceof VidGenApiError) {
    return error.error.detail_code ?? error.code;
  }
  return null;
}

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof VidGenApiError) {
    return error.error.summary;
  }
  return error instanceof Error ? error.message : fallback;
}

/** Hash in a Web Worker, falling back to the same streaming code inline. */
async function runWorkerHash(
  file: File,
  chunkSize: number,
  onProgress: (bytesHashed: number, totalBytes: number) => void,
  signal: AbortSignal,
): Promise<string> {
  if (typeof Worker === "undefined") {
    return hashBlob(file, chunkSize, onProgress);
  }
  const worker = new Worker(new URL("../workers/sha256.worker.ts", import.meta.url), {
    type: "module",
  });
  try {
    return await new Promise<string>((resolve, reject) => {
      const stop = () => worker.terminate();
      signal.addEventListener("abort", () => {
        stop();
        reject(new DOMException("Hashing cancelled", "AbortError"));
      });
      worker.onmessage = (event: MessageEvent<HashResponse>) => {
        const message = event.data;
        if (message.kind === "progress") {
          onProgress(message.bytesHashed, message.totalBytes);
        } else if (message.kind === "done") {
          resolve(message.sha256);
        } else {
          reject(new Error(message.message));
        }
      };
      worker.onerror = () => reject(new Error("The hashing worker failed."));
      worker.postMessage({ kind: "hash", file, chunkSize });
    });
  } finally {
    worker.terminate();
  }
}
