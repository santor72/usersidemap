import type { ExpressionSpecification } from 'maplibre-gl'

type Stop = readonly [zoom: number, value: number]

// MapLibre's 512px zoom is one level above Leaflet's 256px zoom at the
// same geographic scale. These stops therefore apply the legacy rule at z+1.
const nodeRadiusStops: Stop[] = [
  [13, 1], [14, 2], [15, 4], [16, 7], [17, 14], [18, 25], [19, 28], [22, 28]
]
const imageSizeStops: Stop[] = [
  [13, 1], [14, 6], [15, 10], [16, 17], [17, 30], [18, 50], [22, 50]
]

const interpolate = (stops: Stop[], zoom: number): number => {
  if (zoom <= stops[0]![0]) return stops[0]![1]
  for (let index = 1; index < stops.length; index++) {
    const [rightZoom, rightValue] = stops[index]!
    const [leftZoom, leftValue] = stops[index - 1]!
    if (zoom <= rightZoom) return leftValue + (rightValue - leftValue) * (zoom - leftZoom) / (rightZoom - leftZoom)
  }
  return stops[stops.length - 1]![1]
}

const expression = (stops: Stop[], divisor = 1): ExpressionSpecification => [
  'interpolate', ['linear'], ['zoom'],
  ...stops.flatMap(([zoom, value]) => [zoom, value / divisor])
] as ExpressionSpecification

export const legacyNodeRadiusAtMapLibreZoom = (zoom: number) => interpolate(nodeRadiusStops, zoom)
export const legacyImageSizeAtMapLibreZoom = (zoom: number) => interpolate(imageSizeStops, zoom)
export const legacyNodeRadiusExpression = expression(nodeRadiusStops)
export const legacyImageSizeExpression = expression(imageSizeStops, 24)
