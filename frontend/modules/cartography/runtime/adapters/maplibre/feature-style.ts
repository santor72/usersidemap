import type { Feature } from '#map-data'

const legacyTriangle = (feature: Feature): Feature => {
  if (feature.geometry.type !== 'Point') return feature
  const [longitude, latitude] = feature.geometry.coordinates as [number, number]
  const half = 0.00005
  const size = 0.0001
  const round = (coordinate: number) => Math.round(coordinate * 10_000_000) / 10_000_000
  const top: [number, number] = [longitude, round(latitude + half)]
  const left: [number, number] = [round(longitude - size), round(latitude - half)]
  const right: [number, number] = [round(longitude + size), round(latitude - half)]
  return {
    ...feature,
    geometry: { type: 'Polygon', coordinates: [[top, left, right, top]] }
  }
}

export const prepareRenderFeature = (feature: Feature): Feature => {
  const style = feature.properties.style && typeof feature.properties.style === 'object'
    ? feature.properties.style as Record<string, unknown>
    : {}
  // Custom icons are not shipped with the public snapshot; use a built-in symbol.
  const imageFallback = style.shape === 'image'
  const shape = imageFallback ? 'imageFallback' : style.shape
  const renderFeature = shape === 'triangle' ? legacyTriangle(feature) : feature
  return {
    ...renderFeature,
    properties: {
      ...feature.properties,
      __mapId: String(feature.id),
      __mapColor: style.color,
      __mapWidth: style.width,
      __mapDashArray: style.dashArray,
      __mapShape: shape,
      __mapStrokeColor: style.strokeColor,
      __mapStrokeWidth: style.strokeWidth,
      __mapZIndex: style.zIndex,
      __mapImageFallback: imageFallback
    }
  }
}
