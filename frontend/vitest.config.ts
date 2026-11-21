import { defineConfig } from 'vitest/config'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  resolve: { alias: { '#map-data': fileURLToPath(new URL('./modules/map-data/runtime/core/index.ts', import.meta.url)) } }
})
