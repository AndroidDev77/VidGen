import {
  Toast,
  ToastBody,
  ToastTitle,
  useToastController,
} from "@fluentui/react-components";
import { useMemo } from "react";

/** The single app-level toaster mounted by {@link AppProviders}. */
export const APP_TOASTER_ID = "vidgen-app-toaster";

export interface AppToast {
  readonly success: (title: string, body?: string) => void;
  readonly error: (title: string, body?: string) => void;
}

/**
 * Announce the outcome of a background action.
 *
 * Actions like a stage retry finish after the click that started them has left
 * the screen, so their result is reported here rather than by the button.
 */
export function useAppToast(): AppToast {
  const { dispatchToast } = useToastController(APP_TOASTER_ID);
  return useMemo(
    () => ({
      success: (title: string, body?: string) =>
        dispatchToast(
          <Toast>
            <ToastTitle>{title}</ToastTitle>
            {body !== undefined && <ToastBody>{body}</ToastBody>}
          </Toast>,
          { intent: "success" },
        ),
      error: (title: string, body?: string) =>
        dispatchToast(
          <Toast>
            <ToastTitle>{title}</ToastTitle>
            {body !== undefined && <ToastBody>{body}</ToastBody>}
          </Toast>,
          { intent: "error" },
        ),
    }),
    [dispatchToast],
  );
}
