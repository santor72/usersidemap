import { createError, getRequestURL, getRouterParam, proxyRequest, setHeader } from 'h3'

export default defineEventHandler(event => {
  const config = useRuntimeConfig(event)
  const path = getRouterParam(event, 'path') || ''
  const mapId = Number(config.public.mapId)
  // A service credential exposes only this deployed map and read-only metadata/details.
  const routes = /^(\d+)\/tiles\/(catalog|(?:vector|raster)\/[a-f0-9-]{36}\/(?:tilejson\.json|style\.json)|[a-f0-9-]{36}\/objects\/[a-z_]+\/\d+)$/
  const match = routes.exec(path)
  if (event.method !== 'GET' || !match || Number(match[1]) !== mapId) {
    setHeader(event, 'Cache-Control', 'no-store')
    throw createError({ statusCode: 404, statusMessage: 'Map metadata route not found' })
  }
  const token = String(config.mapTilesToken || '')
  const headers: Record<string, string> = token ? { authorization: `Bearer ${token}`, cookie: '' } : {}
  return proxyRequest(event, `${String(config.mapApiInternalUrl).replace(/\/$/, '')}/api/maps/${path}${getRequestURL(event).search}`, { headers })
})
