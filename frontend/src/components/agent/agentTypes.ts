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

// A sub-agent of a team run
export type AgentRunStatus = 'running' | 'done' | 'error' | 'cancelled'

export interface AgentRun {
  id: string
  role: string
  label: string
  title: string
  parentToolId: string
  status: AgentRunStatus
  items: DisplayItem[]
  summary?: string
}

export type AgentMode = 'assistant' | 'team'

// Events streamed by POST /api/agent/chat and /api/agent/team (see backend/agent/runner.py
// and backend/agent/team.py). Team events carry the emitting agent: "lead" or a sub-agent id.
export type AgentEvent = (
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
  | { type: 'session'; session_id: string }
  | { type: 'agent_start'; role: string; label: string; title: string; parent_tool_id: string }
  | { type: 'agent_done'; status: AgentRunStatus; summary: string }
) & { agent?: string }

// Team configuration (see backend/agent/teams.py)
export interface TeamRole {
  id: string
  label: string
  description: string
  instructions: string
  tools: string[]
  model: string  // '' = the assistant's configured model
}

export interface AgentTeam {
  id: string  // '' for a team not saved yet
  name: string
  description: string
  workflow: string
  roles: TeamRole[]
  builtin?: boolean
}

export interface TeamToolInfo {
  name: string
  description: string
  may_need_approval: boolean
}

export interface TeamsInfo {
  teams: AgentTeam[]
  default_team_id: string
  tools: TeamToolInfo[]
  models: string[]
  default_model: string
}
