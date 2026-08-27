/// <reference lib="webworker" />
import { hashBlob, type HashRequest, type HashResponse } from "./sha256";

/**
 * Hash the selected source video off the main thread.
 *
 * The worker receives a `Blob` handle, not bytes: the file is read one bounded
 * slice at a time and never accumulates in memory or in application state.
 */
const scope = self as unknown as DedicatedWorkerGlobalScope;

scope.onmessage = (event: MessageEvent<HashRequest>) => {
  const request = event.data;
  if (request.kind !== "hash") {
    return;
  }
  void hashBlob(request.file, request.chunkSize, (bytesHashed, totalBytes) => {
    const message: HashResponse = { kind: "progress", bytesHashed, totalBytes };
    scope.postMessage(message);
  })
    .then((sha256) => {
      const message: HashResponse = { kind: "done", sha256, totalBytes: request.file.size };
      scope.postMessage(message);
    })
    .catch((error: unknown) => {
      const message: HashResponse = {
        kind: "error",
        message: error instanceof Error ? error.message : "Hashing failed.",
      };
      scope.postMessage(message);
    });
};
