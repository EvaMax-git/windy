/// <reference types="vite/client" />

declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<{}, {}, any>;
  export default component;
}

interface ImportMetaEnv {
  /** Feature flag: enable legacy route redirects (default true) */
  readonly VITE_LEGACY_REDIRECTS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
