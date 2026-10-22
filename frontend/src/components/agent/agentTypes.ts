// Types shared by the AI assistant components

// Conversation history in Anthropic message format. It is owned by the backend
// and sent back unchanged on every request, so it is kept opaque here.
export type ApiMessage = Record<string, unknown>

export type ToolStatus = 'streaming' | 'queued' | 'awaiting' | 'running' | 'done' | 'error' | 'declined'

export type DisplayItem =
  | { kind: 'user'; text: string; context?: string }
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | {
      kind: 'tool'
      id: string
      name: string
      input?: Record<string, unknown>
      status: ToolStatus
      result?: string
      decision?: boolean
    }
  | { kind: 'error'; text: string }
  | { kind: 'notice'; text: string }

export type ToolItem = Extract<DisplayItem, { kind: 'tool' }>

// Events streamed by POST /api/agent/chat (see backend/agent/runner.py)
export type AgentEvent =
  | { type: 'block_start'; index: number; block_type: string; id?: string; name?: string }
  | { type: 'text_delta'; text: string }
  | { type: 'thinking_delta'; text: string }
  | { type: 'tool_use'; id: string; name: string; input: Record<string, unknown> }
  | { type: 'tool_running'; id: string; name: string }
  | { type: 'tool_result'; id: string; name: string; content: string; is_error: boolean }
  | { type: 'project_changed'; tool: string; path?: string }
  | { type: 'approval_required'; tools: { id: string; name: string; input: Record<string, unknown> }[] }
  | { type: 'messages'; messages: ApiMessage[] }
  | { type: 'notice'; message: string }
  | { type: 'error'; message: string }
  | { type: 'done'; stop_reason: string }
