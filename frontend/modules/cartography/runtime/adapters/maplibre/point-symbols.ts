import type { ExpressionSpecification } from 'maplibre-gl'
import type { PointSymbolSettings, Feature } from '../../core/contracts'
import network from 'lucide-static/icons/network.svg?raw'
import link from 'lucide-static/icons/link-2.svg?raw'

/** Add new point types here; unknown types keep their backend geometry/style. */
export const pointIcons: Record<string, string> = { node: network, coupler: link }
export const defaultPointSymbols: PointSymbolSettings = { baseSize: 24, iconMinZoom: 15, labelMinZoom: 16 }
export const normalizePointSymbols = (settings: Partial<PointSymbolSettings> = {}): PointSymbolSettings => ({
  baseSize: Number.isFinite(settings.baseSize) ? Math.max(12, Math.min(48, settings.baseSize!)) : defaultPointSymbols.baseSize,
  iconMinZoom: Number.isFinite(settings.iconMinZoom) ? Math.max(0, Math.min(22, Math.round(settings.iconMinZoom!))) : defaultPointSymbols.iconMinZoom,
  labelMinZoom: Number.isFinite(settings.labelMinZoom) ? Math.max(0, Math.min(22, settings.labelMinZoom!)) : defaultPointSymbols.labelMinZoom
})
export const pointSizeExpression = (baseSize: number, divisor = 1): ExpressionSpecification => [
  'interpolate', ['linear'], ['zoom'], 10, baseSize * 0.5 / divisor, 16, baseSize / divisor, 19, baseSize * 1.25 / divisor
]
export const preparePointSymbol = (feature: Feature): Feature => {
  const type = String(feature.properties.objectType)
  if (!pointIcons[type] || feature.geometry.type !== 'Point') return feature
  const style = feature.properties.style && typeof feature.properties.style === 'object' ? feature.properties.style : {}
  return { ...feature, properties: { ...feature.properties, style: { ...style, shape: 'symbol' }, __mapIcon: `network-icon-${type}` } }
}

/** Rasterize bundled Lucide SVG once at 2x; no DOM markers or icon CDN requests. */
export const rasterizeIcon = async (svg: string): Promise<ImageData> => {
  const image = new Image()
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg.replace('currentColor', '#111827'))}`
  await image.decode()
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 64
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Не удалось создать иконки карты')
  context.drawImage(image, 8, 8, 48, 48)
  return context.getImageData(0, 0, 64, 64)
}
