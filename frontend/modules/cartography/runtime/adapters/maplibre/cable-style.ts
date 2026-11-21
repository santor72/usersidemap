import type { ExpressionSpecification, LineLayerSpecification } from 'maplibre-gl'

export const cableLayerIds = ['cable_line', 'cable_line-dashed'] as const
const lineWidth: ExpressionSpecification = ['coalesce', ['get', '__mapWidth'], 3]
const dashForLegacyPixels = (on: number, off: number): ExpressionSpecification => ['case',
  ['<=', lineWidth, 1], ['literal', [on, off]],
  ['<=', lineWidth, 2], ['literal', [on / 2, off / 2]],
  ['<=', lineWidth, 3], ['literal', [on / 3, off / 3]],
  ['<=', lineWidth, 4], ['literal', [on / 4, off / 4]],
  ['<=', lineWidth, 5], ['literal', [on / 5, off / 5]],
  ['literal', [on / 6, off / 6]]
]

export function cableLayers(source: string): LineLayerSpecification[] {
  const dashed: ExpressionSpecification = ['in', ['get', '__mapDashArray'], ['literal', ['20,15', '1,10']]]
  const paint: LineLayerSpecification['paint'] = {
    'line-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#FFFF59', ['coalesce', ['get', '__mapColor'], '#0877d1']],
    'line-width': lineWidth, 'line-opacity': 1
  }
  const layout: LineLayerSpecification['layout'] = { 'line-cap': 'round', 'line-join': 'round' }
  return [
    // Solid cables must have no dash pattern: a huge gap hides their geometry.
    { id: cableLayerIds[0], type: 'line', source, filter: ['!', dashed], layout, paint },
    { id: cableLayerIds[1], type: 'line', source, filter: dashed, layout,
      paint: { ...paint, 'line-dasharray': ['case',
        ['==', ['get', '__mapDashArray'], '20,15'], dashForLegacyPixels(20, 15),
        dashForLegacyPixels(1, 10)
      ] }
    }
  ]
}

/** Tile prototype shares paint/dash expressions with the object mode. */
export function cableTileLayers(source: string, layerIds: number[] = []): LineLayerSpecification[] {
  const thematic: ExpressionSpecification = layerIds.length
    ? ['any', ...layerIds.map((id): ExpressionSpecification => ['>=', ['index-of', `,${id},`, ['get', 'layerIds']], 0])]
    : ['literal', true]
  return cableLayers(source).map(layer => ({
    ...layer,
    id: `${layer.id}-tiles`,
    'source-layer': 'cable_line',
    filter: ['all', layer.filter as ExpressionSpecification, ['<=', ['get', 'min_zoom'], ['zoom']], thematic],
    layout: { ...layer.layout, 'line-sort-key': ['get', 'renderOrder'] }
  }))
}
