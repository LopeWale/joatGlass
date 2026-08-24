/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * WebSocket URL of the Mac brain. Defaults to wss://mac.local:8787/glasses.
   * The host must ALSO be an exact-match domain in app.json — the Hub silently
   * blocks anything not listed, with no error surfaced to the plugin.
   */
  readonly VITE_MAC_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
