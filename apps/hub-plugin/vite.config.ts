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
    // Pinned AWAY from vite's 5173 default: other projects on this machine
    // use it, and two listeners on one port means the simulator can load the
    // wrong app entirely. strictPort makes a collision fail loudly instead
    // of silently drifting to another port.
    port: 5190,
    strictPort: true,
  },

  build: {
    outDir: 'dist',
    // The plugin runs in the Even app's iOS WebView, not a desktop browser.
    target: 'safari15',
    sourcemap: true,
  },
});
