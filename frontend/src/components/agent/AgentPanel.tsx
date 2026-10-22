// AI assistant panel: chat with an agent (or a team of agents) that can explore, edit, compile,
// test and run the dbt project

import { useState, useEffect, useRef, KeyboardEvent } from 'react'
import { Sparkles, X, MessageSquarePlus, Send, Square, FileText, AlertTriangle, Users, Bot } from 'lucide-react'
import { apiUrl, apiFetch } from '../../config/api'
import { AgentEvent, AgentMode, AgentRun, ApiMessage, DisplayItem, ToolItem } from './agentTypes'
import { applyItemEvent, interruptTools } from './agentEvents'
import AgentItems from './AgentItems'
import './AgentPanel.css'

const AUTO_APPROVE_KEY = 'dbt-ui-agent-auto-approve'
const MODE_KEY = 'dbt-ui-agent-mode'

const SUGGESTIONS_WITH_FILE = [
  'Explain what this model does and how it fits in the lineage',
  'Add documentation and tests for this model',
  'Review this model for bugs and performance issues',
]
const SUGGESTIONS = [
  'Give me an overview of this dbt project',
  'Which models have no tests or documentation?',
  'Compile the project and fix any errors',
]
const TEAM_SUGGESTIONS_WITH_FILE = [
  'Refactor this model test-first: write unit tests, then implement, then review',
  'Add tests and documentation for this model and everything downstream of it',
]
const TEAM_SUGGESTIONS = [
  'Add tests and documentation to every model in the staging layer',
  'Create a mart model for monthly revenue per customer, test-first',
  'Review the marts layer and fix what the reviewer finds',
]

interface AgentPanelProps {
  projectPath: string
  selectedFile: string | null
  selectedTarget: string
  onClose: () => void
  // Called when the agent changed files or ran a dbt command
  onProjectChanged: (changedPath?: string) => void
}

function readPreference(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writePreference(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Preference is just not remembered
  }
}

function setToolDecision(items: DisplayItem[], toolId: string, approved: boolean): DisplayItem[] {
  return items.map(item => (item.kind === 'tool' && item.id === toolId ? { ...item, decision: approved } : item))
}

function AgentPanel({ projectPath, selectedFile, selectedTarget, onClose, onProjectChanged }: AgentPanelProps) {
  const [mode, setMode] = useState<AgentMode>(() => (readPreference(MODE_KEY) === 'team' ? 'team' : 'assistant'))
  const [history, setHistory] = useState<ApiMessage[]>([])
  const [items, setItems] = useState<DisplayItem[]>([])
  const [agents, setAgents] = useState<AgentRun[]>([])
  const [input, setInput] = useState('')
  const [isRunning, setIsRunning] = useState(false)
  const [includeFile, setIncludeFile] = useState(true)
  const [status, setStatus] = useState<{ configured: boolean; model: string } | null>(null)
  const [autoApprove, setAutoApprove] = useState(() => readPreference(AUTO_APPROVE_KEY) === 'true')
  const abortRef = useRef<AbortController | null>(null)
  const sessionRef = useRef<string | null>(null)  // Team run id, for approvals and cancellation
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const historyRef = useRef<ApiMessage[]>([])
  historyRef.current = history

  useEffect(() => {
    apiFetch(apiUrl('/api/agent/status'))
      .then(response => (response.ok ? response.json() : null))
      .then(data => data && setStatus(data))
      .catch(err => console.error('[AgentPanel] Error loading assistant status:', err))
  }, [])

  const stop = () => {
    if (sessionRef.current) {
      apiFetch(apiUrl('/api/agent/cancel'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionRef.current }),
      }).catch(err => console.error('[AgentPanel] Error cancelling team run:', err))
    }
    abortRef.current?.abort()
  }

  const newConversation = () => {
    stop()
    setHistory([])
    setItems([])
    setAgents([])
  }

  // New project: start a new conversation
  useEffect(() => {
    newConversation()
  }, [projectPath])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [items, agents])

  const toggleAutoApprove = () => {
    setAutoApprove(!autoApprove)
    writePreference(AUTO_APPROVE_KEY, String(!autoApprove))
  }

  // The two modes use different tools, so switching starts a new conversation
  const switchMode = (next: AgentMode) => {
    if (next === mode || isRunning) return
    newConversation()
    setMode(next)
    writePreference(MODE_KEY, next)
  }

  const handleEvent = (event: AgentEvent) => {
    const agentId = event.agent && event.agent !== 'lead' ? event.agent : null

    if (event.type === 'agent_start' && agentId) {
      setAgents(prev => [...prev, {
        id: agentId, role: event.role, label: event.label, title: event.title,
        parentToolId: event.parent_tool_id, status: 'running', items: [],
      }])
      return
    }
    if (event.type === 'agent_done' && agentId) {
      setAgents(prev => prev.map(agent => (agent.id === agentId
        ? { ...agent, status: event.status, summary: event.summary, items: interruptTools(agent.items) }
        : agent)))
      return
    }
    if (event.type === 'session') {
      sessionRef.current = event.session_id
      return
    }
    if (event.type === 'project_changed') {
      onProjectChanged(event.path)
      return
    }
    if (event.type === 'messages') {
      setHistory(event.messages)
      return
    }

    if (agentId) {
      setAgents(prev => prev.map(agent => (agent.id === agentId ? { ...agent, items: applyItemEvent(agent.items, event) } : agent)))
    } else {
      setItems(prev => applyItemEvent(prev, event))
    }
  }

  const runAgent = async (body: { user_message?: string; approvals?: Record<string, boolean>; selected_file?: string }) => {
    const controller = new AbortController()
    abortRef.current = controller
    sessionRef.current = null
    setIsRunning(true)

    try {
      const response = await apiFetch(apiUrl(mode === 'team' ? '/api/agent/team' : '/api/agent/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        signal: controller.signal,
        body: JSON.stringify({
          path: projectPath,
          messages: historyRef.current,
          target: selectedTarget,
          auto_approve: autoApprove,
          ...body,
        }),
      })

      if (!response.ok || !response.body) {
        const detail = await response.json().catch(() => null)
        handleEvent({ type: 'error', message: detail?.detail || `Request failed (${response.status})` })
        return
      }

      // Parse the Server-Sent Events stream
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) {
        buffer += decoder.decode(chunk.value, { stream: true })
        const events = buffer.split('\n\n')
        buffer = events.pop() ?? ''
        for (const rawEvent of events) {
          const data = rawEvent.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('')
          if (data) handleEvent(JSON.parse(data) as AgentEvent)
        }
      }
    } catch (err) {
      if (controller.signal.aborted) {
        handleEvent({ type: 'notice', message: 'Stopped.' })
      } else {
        console.error('[AgentPanel] Error running assistant:', err)
        handleEvent({ type: 'error', message: 'Error connecting to backend' })
      }
    } finally {
      // Work left unfinished by an interruption
      setItems(prev => interruptTools(prev))
      setAgents(prev => prev.map(agent => (agent.status === 'running'
        ? { ...agent, status: 'cancelled', items: interruptTools(agent.items) }
        : agent)))
      setIsRunning(false)
      abortRef.current = null
      sessionRef.current = null
    }
  }

  const sendMessage = (text: string) => {
    const message = text.trim()
    if (!message || isRunning) return
    const contextFile = includeFile && selectedFile ? selectedFile : undefined
    // A new message while actions await approval declines them
    setItems(prev => [
      ...prev.map(item => (item.kind === 'tool' && item.status === 'awaiting' ? { ...item, status: 'declined' as const } : item)),
      { kind: 'user', text: message, context: contextFile },
    ])
    setInput('')
    runAgent({ user_message: message, selected_file: contextFile })
  }

  // Assistant mode: the run has stopped; resume it once every pending call has a decision
  const handleAssistantDecision = (toolId: string, approved: boolean) => {
    const updated = setToolDecision(items, toolId, approved)
    setItems(updated)
    const awaiting = updated.filter((item): item is ToolItem => item.kind === 'tool' && item.status === 'awaiting')
    if (awaiting.every(item => item.decision !== undefined)) {
      const approvals = Object.fromEntries(awaiting.map(item => [item.id, item.decision === true]))
      runAgent({ approvals })
    }
  }

  // Team mode: the agent is blocked waiting; send the decision to the live run
  const handleTeamDecision = (toolId: string, approved: boolean) => {
    if (!sessionRef.current) return
    setItems(prev => setToolDecision(prev, toolId, approved))
    setAgents(prev => prev.map(agent => ({ ...agent, items: setToolDecision(agent.items, toolId, approved) })))
    apiFetch(apiUrl('/api/agent/approve'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionRef.current, tool_id: toolId, approved }),
    }).catch(err => console.error('[AgentPanel] Error sending approval:', err))
  }

  const onDecision = mode === 'team'
    ? (isRunning ? handleTeamDecision : undefined)
    : (isRunning ? undefined : handleAssistantDecision)

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  const withFile = Boolean(selectedFile && includeFile)
  const suggestions = mode === 'team'
    ? (withFile ? TEAM_SUGGESTIONS_WITH_FILE : TEAM_SUGGESTIONS)
    : (withFile ? SUGGESTIONS_WITH_FILE : SUGGESTIONS)

  return (
    <div className="agent-panel">
      <div className="agent-header">
        <Sparkles size={15} className="agent-header-icon" />
        <div className="agent-mode-toggle" title={isRunning ? 'Stop the current run to switch mode' : undefined}>
          <button className={mode === 'assistant' ? 'active' : ''} onClick={() => switchMode('assistant')} disabled={isRunning}>
            <Bot size={12} /> Assistant
          </button>
          <button className={mode === 'team' ? 'active' : ''} onClick={() => switchMode('team')} disabled={isRunning}>
            <Users size={12} /> Team
          </button>
        </div>
        <span className="agent-header-spacer" />
        <label className="agent-auto-approve" title="Apply file changes and run dbt run/seed/build without asking">
          <input type="checkbox" checked={autoApprove} onChange={toggleAutoApprove} />
          Auto-approve
        </label>
        <button className="agent-icon-btn" onClick={newConversation} title="New conversation">
          <MessageSquarePlus size={15} />
        </button>
        <button className="agent-icon-btn" onClick={onClose} title="Close assistant">
          <X size={15} />
        </button>
      </div>

      {status && !status.configured && (
        <div className="agent-warning">
          <AlertTriangle size={14} />
          <span>No Anthropic credentials found. Set <code>ANTHROPIC_API_KEY</code> for the backend.</span>
        </div>
      )}

      <div className="agent-messages" ref={scrollRef}>
        {items.length === 0 && (
          <div className="agent-empty">
            {mode === 'team' ? <Users size={28} /> : <Sparkles size={28} />}
            <p>
              {mode === 'team'
                ? 'A lead agent plans the work per model and dispatches test writers, developers, reviewers and documenters that work in parallel, test-first.'
                : 'Ask about your project, or have the assistant write models, tests and docs, then compile and validate them.'}
            </p>
            <div className="agent-suggestions">
              {suggestions.map(suggestion => (
                <button key={suggestion} className="agent-suggestion" onClick={() => sendMessage(suggestion)}>
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        <AgentItems items={items} agents={agents} onDecision={onDecision} />

        {isRunning && <div className="agent-working"><span /><span /><span /></div>}
      </div>

      <div className="agent-input-area">
        {selectedFile && (
          <button
            className={`agent-context-chip ${includeFile ? 'active' : ''}`}
            onClick={() => setIncludeFile(!includeFile)}
            title={includeFile ? 'The open file is shared with the assistant (click to stop)' : 'Click to share the open file with the assistant'}
          >
            <FileText size={11} /> {selectedFile.split('/').pop()}
          </button>
        )}
        <div className="agent-input-row">
          <textarea
            className="agent-input"
            placeholder={mode === 'team' ? 'Describe the work for the team…' : 'Ask the assistant…'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={3}
          />
          {isRunning ? (
            <button className="agent-send-btn" onClick={stop} title="Stop">
              <Square size={14} />
            </button>
          ) : (
            <button className="agent-send-btn" onClick={() => sendMessage(input)} disabled={!input.trim()} title="Send (Enter)">
              <Send size={14} />
            </button>
          )}
        </div>
        {status?.model && <div className="agent-footer">{status.model}</div>}
      </div>
    </div>
  )
}

export default AgentPanel
