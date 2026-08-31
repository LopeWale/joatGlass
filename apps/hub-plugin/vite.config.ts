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
    proxy: {
      // Design-QA capture hook: the browser preview (?joatPreview=browser)
      // POSTs its rendered 576x288 frame here, same-origin, and a throwaway
      // local receiver on 5191 writes the PNG. Dev-server only — the packed
      // plugin never sees this config, and with no receiver running the
      // proxy simply 502s.
      '/__joat-hud-save': {
        target: 'http://localhost:5191',
        changeOrigin: true,
        rewrite: (path) => path.replace('/__joat-hud-save', '/'),
      },
    },
  },

  build: {
    outDir: 'dist',
    // The plugin runs in the Even app's iOS WebView, not a desktop browser.
    target: 'safari15',
    // DEV visual fixtures must not be recoverable from a production map.
    sourcemap: false,
  },
});
