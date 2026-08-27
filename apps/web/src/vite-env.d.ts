/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_VIDGEN_API_BASE_URL?: string;
  readonly VITE_VIDGEN_DEV_USER?: string;
  readonly DEV: boolean;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
