import { createResolver, defineNuxtModule, addPlugin } from '@nuxt/kit'

export default defineNuxtModule({
  meta: { name: 'userside-map-data', configKey: 'mapData' },
  setup(_options, nuxt) {
    const resolver = createResolver(import.meta.url)
    nuxt.options.alias['#map-data'] = resolver.resolve('./runtime/core/index')
    addPlugin(resolver.resolve('./runtime/plugin'))
  }
})
