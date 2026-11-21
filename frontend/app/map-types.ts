import type { ObjectType } from '#map-data'

/** Add application object kinds here after registering the matching backend provider. */
export const mapObjectTypes: Array<{ id: ObjectType; title: string }> = [
  { id: 'cable_line', title: 'Кабели' },
  { id: 'node', title: 'Узлы' },
  { id: 'coupler', title: 'Муфты' }
]
