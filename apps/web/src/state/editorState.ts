import { useCallback, useMemo, useRef, useState } from "react";
import type { MutableRefObject } from "react";

/**
 * Local state for *unsaved* editor text only.
 *
 * Server state stays in TanStack Query; this never becomes a second copy of it.
 * When a save conflicts, the local draft is kept so the user can compare it
 * against the refreshed server value instead of silently losing the edit.
 */
export interface DraftState<TValue> {
  readonly drafts: ReadonlyMap<string, TValue>;
  readonly isDirty: boolean;
  readonly conflicts: ReadonlyMap<string, TValue>;
  get: (id: string) => TValue | undefined;
  set: (id: string, value: TValue) => void;
  clear: (id: string) => void;
  clearAll: () => void;
  markConflict: (id: string, value: TValue) => void;
  resolveConflict: (id: string) => void;
}

export function useDraftState<TValue>(): DraftState<TValue> {
  const [drafts, setDrafts] = useState<ReadonlyMap<string, TValue>>(new Map());
  const [conflicts, setConflicts] = useState<ReadonlyMap<string, TValue>>(new Map());

  const set = useCallback((id: string, value: TValue) => {
    setDrafts((previous) => new Map(previous).set(id, value));
  }, []);

  const clear = useCallback((id: string) => {
    setDrafts((previous) => {
      const next = new Map(previous);
      next.delete(id);
      return next;
    });
  }, []);

  const clearAll = useCallback(() => setDrafts(new Map()), []);

  const markConflict = useCallback((id: string, value: TValue) => {
    setConflicts((previous) => new Map(previous).set(id, value));
  }, []);

  const resolveConflict = useCallback((id: string) => {
    setConflicts((previous) => {
      const next = new Map(previous);
      next.delete(id);
      return next;
    });
  }, []);

  const get = useCallback((id: string) => drafts.get(id), [drafts]);

  return useMemo(
    () => ({
      drafts,
      conflicts,
      isDirty: drafts.size > 0,
      get,
      set,
      clear,
      clearAll,
      markConflict,
      resolveConflict,
    }),
    [drafts, conflicts, get, set, clear, clearAll, markConflict, resolveConflict],
  );
}

/**
 * Track unsaved editor text so a navigation guard can read it without
 * re-registering a listener on every keystroke.
 */
export function useUnsavedChangesRef(isDirty: boolean): MutableRefObject<boolean> {
  const ref = useRef(isDirty);
  ref.current = isDirty;
  return ref;
}
