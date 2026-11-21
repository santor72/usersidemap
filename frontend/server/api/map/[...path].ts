import { getRequestURL, getRouterParam, proxyRequest } from 'h3'

export default defineEventHandler((event) => {
  const config = useRuntimeConfig(event)
  const path = getRouterParam(event, 'path') || ''
  const query = getRequestURL(event).search
  return proxyRequest(event, `${String(config.mapApiInternalUrl).replace(/\/$/, '')}/api/map/${path}${query}`, config.mapTilesToken ? { headers: { authorization: `Bearer ${config.mapTilesToken}`, cookie: '' } } : undefined)
})
