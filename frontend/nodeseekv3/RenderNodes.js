import { h, resolveDynamicComponent } from 'vue'

export function renderNodes(nodes, { model, bindings = {}, actions = {}, update, submit } = {}) {
  function render(node, key) {
    if (node.show && !bindings[node.show]) return null

    const { model: field, ...props } = node.props || {}
    for (const [name, binding] of Object.entries(node.bind || {})) {
      props[name] = bindings[binding]
    }
    if (field) {
      props.modelValue = model[field]
      props['onUpdate:modelValue'] = value => update(field, value)
    }
    if (node.action) props.onClick = actions[node.action]
    if (node.component === 'VForm') {
      props.onSubmit = event => {
        event.preventDefault()
        submit()
      }
    }

    const component = resolveDynamicComponent(node.component)
    const children = node.content ? () => node.content.map(render) : node.text !== undefined ? () => node.text : undefined
    return h(component, { ...props, key }, typeof component === 'string' ? children?.() : children && { default: children })
  }

  return nodes.map(render)
}
