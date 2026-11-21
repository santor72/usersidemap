import type { MapAdapterFactory } from './core/contracts'

export default defineNuxtPlugin(() => {
  const createMapAdapter: MapAdapterFactory = async (container, options) => {
    const { MapLibreAdapter } = await import('./adapters/maplibre/adapter')
    return new MapLibreAdapter(container, options)
  }
  return { provide: { createMapAdapter, cartography: { version: '1', adapters: ['maplibre'] } } }
})
