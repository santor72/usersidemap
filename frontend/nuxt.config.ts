export default defineNuxtConfig({
  compatibilityDate: '2025-09-15',
  devtools: { enabled: false },
  modules: ['./modules/map-data/module', './modules/cartography/module'],
  css: ['maplibre-gl/dist/maplibre-gl.css', '~/assets/css/main.css'],
  runtimeConfig: {
    mapApiInternalUrl: process.env.NUXT_MAP_API_INTERNAL_URL || process.env.MAP_API_INTERNAL_URL || 'http://api:8000',
    mapTilesToken: process.env.NUXT_MAP_TILES_TOKEN || '',
    public: {
      cableTiles: process.env.NUXT_PUBLIC_CABLE_TILES || process.env.CABLE_TILES || 'none',
      mapApiBase: process.env.NUXT_PUBLIC_MAP_API_BASE || '/api/map',
      mapId: Number(process.env.NUXT_PUBLIC_MAP_ID || 3),
      mapLongitude: Number(process.env.NUXT_PUBLIC_MAP_LONGITUDE || 37.664),
      mapLatitude: Number(process.env.NUXT_PUBLIC_MAP_LATITUDE || 55.76005),
      mapIconBaseSize: Number(process.env.NUXT_PUBLIC_MAP_ICON_BASE_SIZE || 24),
      mapIconMinZoom: Number(process.env.NUXT_PUBLIC_MAP_ICON_MIN_ZOOM ?? 15),
      mapLabelMinZoom: Number(process.env.NUXT_PUBLIC_MAP_LABEL_MIN_ZOOM || 16),
      mapZoom: Number(process.env.NUXT_PUBLIC_MAP_ZOOM || 15),
      mapTilesUrl: process.env.NUXT_PUBLIC_MAP_TILES_URL || 'https://tile2.maps.2gis.com/tiles?x={x}&y={y}&z={z}',
      mapAttribution: process.env.NUXT_PUBLIC_MAP_ATTRIBUTION || '© 2GIS'
    }
  },
  typescript: { strict: true, typeCheck: true }
})
