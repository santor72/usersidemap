import { MapDataClient } from './core/client'
import type { MapDataClient as Client } from './core/client'

export default defineNuxtPlugin(() => {
  const baseUrl = String(useRuntimeConfig().public.mapApiBase)
  return { provide: { createMapDataClient: () => new MapDataClient(baseUrl) } }
})

declare module '#app' { interface NuxtApp { $createMapDataClient: () => Client } }
declare module 'vue' { interface ComponentCustomProperties { $createMapDataClient: () => Client } }
