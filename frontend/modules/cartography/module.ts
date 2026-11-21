import { createResolver, defineNuxtModule, addComponentsDir, addPlugin } from '@nuxt/kit'

export default defineNuxtModule({
  meta: { name: 'userside-cartography', configKey: 'cartography' },
  setup(_options, nuxt) {
    const resolver = createResolver(import.meta.url)
    nuxt.options.alias['#cartography'] = resolver.resolve('./runtime/core/index')
    addComponentsDir({ path: resolver.resolve('./runtime/components') })
    addPlugin(resolver.resolve('./runtime/plugin'))
  }
})
