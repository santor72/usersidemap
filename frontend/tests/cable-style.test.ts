import { describe, expect, it } from 'vitest'
import { createExpression } from '@maplibre/maplibre-gl-style-spec'
import { cableLayers, cableTileLayers } from '../modules/cartography/runtime/adapters/maplibre/cable-style'

const layers = cableLayers('objects-cable_line')
function evaluate(expression: unknown, properties: Record<string, unknown>) {
  const parsed = createExpression(expression, 'layers[0].filter')
  if (parsed.result === 'error') throw new Error(JSON.stringify(parsed.value))
  return parsed.value.evaluate({ zoom: 15 }, { type: 'LineString', properties })
}

describe('cable rendering', () => {
  it('filters tile themes by delimited IDs and applies min_zoom under overzoom', () => {
    const tileLayers = cableTileLayers('cables', [1])
    expect(tileLayers[0]!['source-layer']).toBe('cable_line')
    expect(tileLayers[0]!.paint).not.toHaveProperty('line-dasharray')
    expect(evaluate(tileLayers[0]!.filter, { layerIds: ',11,', min_zoom: 14 })).toBe(false)
    expect(evaluate(tileLayers[0]!.filter, { layerIds: ',1,11,', min_zoom: 14 })).toBe(true)
    expect(evaluate(tileLayers[0]!.filter, { layerIds: ',1,', min_zoom: 16 })).toBe(false)
    expect(evaluate(cableTileLayers('cables')[0]!.filter, { layerIds: ',', min_zoom: 14 })).toBe(true)
  })
  it('renders ordinary cables as continuous lines without a dash shader', () => {
    const solid = layers[0]!
    expect(solid.paint).not.toHaveProperty('line-dasharray')
    for (const dash of [undefined, '', 'unknown']) {
      const properties = { __mapDashArray: dash }
      expect(evaluate(solid.filter, properties)).toBe(true)
      expect(evaluate(layers[1]!.filter, properties)).toBe(false)
    }
  })
  it('keeps legacy dash patterns only for explicitly dashed cables', () => {
    for (const dash of ['20,15', '1,10']) {
      const properties = { __mapDashArray: dash, __mapWidth: 5 }
      expect(evaluate(layers[0]!.filter, properties)).toBe(false)
      expect(evaluate(layers[1]!.filter, properties)).toBe(true)
      expect(evaluate(layers[1]!.paint!['line-dasharray'], properties)).toEqual(dash === '20,15' ? [4,3] : [0.2,2])
    }
  })
})
