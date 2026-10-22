// Applies streamed agent events to a list of display items

import { AgentEvent, DisplayItem, ToolItem } from './agentTypes'

export const DECLINED_MESSAGE = 'The user declined this action.'

function updateTool(items: DisplayItem[], id: string, update: Partial<ToolItem>): DisplayItem[] {
  return items.map(item => (item.kind === 'tool' && item.id === id ? { ...item, ...update } : item))
}

// Append streamed text to the latest item of the given kind
function appendToLast(items: DisplayItem[], kind: 'text' | 'thinking', text: string): DisplayItem[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]
    if (item.kind === kind) {
      const next = [...items]
      next[i] = { ...item, text: item.text + text }
      return next
    }
  }
  return [...items, { kind, text }]
}

// Returns the new items, or the same array for events that do not affect items
export function applyItemEvent(items: DisplayItem[], event: AgentEvent): DisplayItem[] {
  switch (event.type) {
    case 'block_start':
      if (event.block_type === 'text') return [...items, { kind: 'text', text: '' }]
      if (event.block_type === 'thinking') return [...items, { kind: 'thinking', text: '' }]
      if (event.block_type === 'tool_use' && event.id && event.name) {
        return [...items, { kind: 'tool', id: event.id, name: event.name, status: 'streaming' }]
      }
      return items
    case 'text_delta':
      return appendToLast(items, 'text', event.text)
    case 'thinking_delta':
      return appendToLast(items, 'thinking', event.text)
    case 'tool_use':
      return updateTool(items, event.id, { input: event.input, status: 'queued' })
    case 'approval_required':
      return event.tools.reduce((acc, tool) => updateTool(acc, tool.id, { status: 'awaiting' }), items)
    case 'tool_running':
      return updateTool(items, event.id, { status: 'running' })
    case 'tool_result': {
      const declined = event.is_error && event.content === DECLINED_MESSAGE
      return updateTool(items, event.id, { status: declined ? 'declined' : event.is_error ? 'error' : 'done', result: event.content })
    }
    case 'notice':
      return [...items, { kind: 'notice', text: event.message }]
    case 'error':
      return [...items, { kind: 'error', text: event.message }]
    default:
      return items
  }
}

// Marks tool calls left unfinished by an interruption
export function interruptTools(items: DisplayItem[]): DisplayItem[] {
  return items.map(item =>
    item.kind === 'tool' && ['streaming', 'queued', 'running', 'awaiting'].includes(item.status)
      ? { ...item, status: 'error', result: item.result ?? 'Interrupted' }
      : item,
  )
}
