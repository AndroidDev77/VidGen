import { useMutation, useQueryClient, type UseMutationResult } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { newIdempotencyKey } from "../api/client";
import { VidGenApiError } from "../api/errors";

export interface VersionedMutationOptions<TInput, TResult> {
  /** A stable prefix for the generated `Idempotency-Key`. */
  readonly operation: string;
  readonly mutate: (input: TInput, idempotencyKey: string) => Promise<TResult>;
  /** Query keys to invalidate on success. Nothing broader is touched. */
  readonly invalidates?: (result: TResult, input: TInput) => readonly (readonly unknown[])[];
  readonly onSuccess?: (result: TResult, input: TInput) => void;
}

export type VersionedMutation<TInput, TResult> = UseMutationResult<
  TResult,
  unknown,
  TInput
> & {
  /** The server's current row version when the last attempt conflicted. */
  readonly conflictVersion: number | null;
  readonly clearConflict: () => void;
};

/**
 * Run one versioned, idempotent mutation.
 *
 * The key is generated once per logical submission and reused across the
 * request's lifetime, so a double-click can never apply the change twice. On a
 * conflict the caller keeps the local draft and is handed the current version.
 */
export function useVersionedMutation<TInput, TResult>(
  options: VersionedMutationOptions<TInput, TResult>,
): VersionedMutation<TInput, TResult> {
  const queryClient = useQueryClient();
  const [conflictVersion, setConflictVersion] = useState<number | null>(null);

  const mutation = useMutation<TResult, unknown, TInput>({
    mutationFn: async (input: TInput) => {
      setConflictVersion(null);
      return options.mutate(input, newIdempotencyKey(options.operation));
    },
    onSuccess: (result, input) => {
      for (const queryKey of options.invalidates?.(result, input) ?? []) {
        void queryClient.invalidateQueries({ queryKey });
      }
      options.onSuccess?.(result, input);
    },
    onError: (error) => {
      if (error instanceof VidGenApiError && error.currentVersion !== null) {
        setConflictVersion(error.currentVersion);
      }
    },
    // A mutation is never auto-retried; the idempotency key makes a *deliberate*
    // retry safe, but an automatic one would hide real failures.
    retry: false,
  });

  const clearConflict = useCallback(() => setConflictVersion(null), []);

  return Object.assign(mutation, { conflictVersion, clearConflict });
}
