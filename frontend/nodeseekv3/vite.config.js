import { fileURLToPath } from 'node:url'
import federation from '@originjs/vite-plugin-federation'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'NodeSeekV3',
      filename: 'remoteEntry.js',
      exposes: {
        './Config': './Config.vue',
        './Page': './Page.vue',
      },
      shared: {
        vue: { import: false, generate: false },
      },
    }),
  ],
  build: {
    outDir: fileURLToPath(new URL('../../plugins.v3/nodeseekv3/dist', import.meta.url)),
    assetsDir: 'assets/v5',
    emptyOutDir: true,
    target: 'esnext',
  },
})
