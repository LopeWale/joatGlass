import { defineConfig } from 'vite';

export default defineConfig({
  // Relative, not '/'. The Hub serves the packed plugin from a path we do not
  // control, so absolute asset URLs resolve against the wrong root and the
  // bundle silently fails to load.
  base: './',

  server: {
    // Bind all interfaces so the phone can reach the dev server over the LAN
    // while the plugin runs inside the Even app WebView.
    host: true,
    port: 5173,
  },

  build: {
    outDir: 'dist',
    // The plugin runs in the Even app's iOS WebView, not a desktop browser.
    target: 'safari15',
    sourcemap: true,
  },
});
