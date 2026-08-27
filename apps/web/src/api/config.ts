/**
 * Validated browser configuration.
 *
 * Only non-secret values are read here. Server secrets are never exposed
 * through `VITE_` variables, and the development identity below is a local
 * convenience, not an authentication mechanism.
 */
export interface WebConfig {
  /** Empty means "same origin", which is what the Vite dev proxy provides. */
  readonly apiBaseUrl: string;
  readonly devUser: string;
  readonly isDevelopment: boolean;
}

function readBaseUrl(raw: unknown): string {
  if (typeof raw !== "string" || raw.trim() === "") {
    return "";
  }
  const value = raw.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//.test(value)) {
    throw new Error(
      "VITE_VIDGEN_API_BASE_URL must be an absolute http(s) URL, or empty to use the dev proxy.",
    );
  }
  return value;
}

export function readConfig(env: ImportMetaEnv = import.meta.env): WebConfig {
  const devUser = env.VITE_VIDGEN_DEV_USER;
  return {
    apiBaseUrl: readBaseUrl(env.VITE_VIDGEN_API_BASE_URL),
    devUser: typeof devUser === "string" && devUser.trim() !== "" ? devUser.trim() : "local-user",
    isDevelopment: env.DEV === true,
  };
}
