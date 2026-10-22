// AI assistant panel: chat with an agent that can explore, edit, compile, test and run the dbt project

import { useState, useEffect, useRef, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import { Sparkles, X, MessageSquarePlus, Send, Square, Brain, ChevronRight, ChevronDown, FileText, AlertTriangle } from 'lucide-react'
import { apiUrl, apiFetch } from '../../config/api'
import { AgentEvent, ApiMessage, DisplayItem, ToolItem } from './agentTypes'
import ToolCallCard from './ToolCallCard'
import './AgentPanel.css'

const AUTO_APPROVE_KEY = 'dbt-ui-agent-auto-approve'

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

interface AgentPanelProps {
  projectPath: string
  selectedFile: string | null
  selectedTarget: string
  onClose: () => void
  // Called when the agent changed files or ran a dbt command
  onProjectChanged: (changedPath?: string) => void
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  if (!text.trim()) return null
  return (
    <div className="agent-thinking">
      <button className="agent-thinking-toggle" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Brain size={12} /> Thinking
      </button>
      {open && <div className="agent-thinking-text">{text}</div>}
    </div>
  )
}

function AgentPanel({ projectPath, selectedFile, selectedTarget, onClose, onProjectChanged }: AgentPanelProps) {
  const [history, setHistory] = useState<ApiMessage[]>([])
  const [items, setItems] = useState<DisplayItem[]>([])
  const [input, setInput] = useState('')
  const [isRunning, setIsRunning] = useState(false)
  const [includeFile, setIncludeFile] = useState(true)
  const [status, setStatus] = useState<{ configured: boolean; model: string } | null>(null)
  const [autoApprove, setAutoApprove] = useState(() => {
    try {
      return localStorage.getItem(AUTO_APPROVE_KEY) === 'true'
    } catch {
      return false
    }
  })
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const historyRef = useRef<ApiMessage[]>([])
  historyRef.current = history

  useEffect(() => {
    apiFetch(apiUrl('/api/agent/status'))
      .then(response => (response.ok ? response.json() : null))
      .then(data => data && setStatus(data))
      .catch(err => console.error('[AgentPanel] Error loading assistant status:', err))
  }, [])

  // New project: start a new conversation
  useEffect(() => {
    abortRef.current?.abort()
    setHistory([])
    setItems([])
  }, [projectPath])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [items])

  const toggleAutoApprove = () => {
    const next = !autoApprove
    setAutoApprove(next)
    try {
      localStorage.setItem(AUTO_APPROVE_KEY, String(next))
    } catch {
      // Preference is just not remembered
    }
  }

  const updateTool = (id: string, update: Partial<ToolItem>) => {
    setItems(prev => prev.map(item => (item.kind === 'tool' && item.id === id ? { ...item, ...update } : item)))
  }

  // Append streamed text to the latest item of the given kind
  const appendToLast = (kind: 'text' | 'thinking', text: string) => {
    setItems(prev => {
      for (let i = prev.length - 1; i >= 0; i--) {
        const item = prev[i]
        if (item.kind === kind) {
          const next = [...prev]
          next[i] = { ...item, text: item.text + text }
          return next
        }
      }
      return [...prev, { kind, text }]
    })
  }

  const handleEvent = (event: AgentEvent) => {
    switch (event.type) {
      case 'block_start':
        if (event.block_type === 'text') {
          setItems(prev => [...prev, { kind: 'text', text: '' }])
        } else if (event.block_type === 'thinking') {
          setItems(prev => [...prev, { kind: 'thinking', text: '' }])
        } else if (event.block_type === 'tool_use' && event.id && event.name) {
          setItems(prev => [...prev, { kind: 'tool', id: event.id!, name: event.name!, status: 'streaming' }])
        }
        break
      case 'text_delta':
        appendToLast('text', event.text)
        break
      case 'thinking_delta':
        appendToLast('thinking', event.text)
        break
      case 'tool_use':
        updateTool(event.id, { input: event.input, status: 'queued' })
        break
      case 'approval_required':
        event.tools.forEach(tool => updateTool(tool.id, { status: 'awaiting' }))
        break
      case 'tool_running':
        updateTool(event.id, { status: 'running' })
        break
      case 'tool_result': {
        const declined = event.is_error && event.content === 'The user declined this action.'
        updateTool(event.id, { status: declined ? 'declined' : event.is_error ? 'error' : 'done', result: event.content })
        break
      }
      case 'project_changed':
        onProjectChanged(event.path)
        break
      case 'messages':
        setHistory(event.messages)
        break
      case 'notice':
        setItems(prev => [...prev, { kind: 'notice', text: event.message }])
        break
      case 'error':
        setItems(prev => [...prev, { kind: 'error', text: event.message }])
        break
      case 'done':
        break
    }
  }

  const runAgent = async (body: { user_message?: string; approvals?: Record<string, boolean>; selected_file?: string }) => {
    const controller = new AbortController()
    abortRef.current = controller
    setIsRunning(true)

    try {
      const response = await apiFetch(apiUrl('/api/agent/chat'), {
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
      // Tool calls left unfinished by an interruption
      setItems(prev =>
        prev.map(item =>
          item.kind === 'tool' && ['streaming', 'queued', 'running'].includes(item.status)
            ? { ...item, status: 'error', result: item.result ?? 'Interrupted' }
            : item,
        ),
      )
      setIsRunning(false)
      abortRef.current = null
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

  const handleDecision = (toolId: string, approved: boolean) => {
    const updated = items.map(item => (item.kind === 'tool' && item.id === toolId ? { ...item, decision: approved } : item))
    setItems(updated)
    const awaiting = updated.filter((item): item is ToolItem => item.kind === 'tool' && item.status === 'awaiting')
    if (awaiting.every(item => item.decision !== undefined)) {
      const approvals = Object.fromEntries(awaiting.map(item => [item.id, item.decision === true]))
      runAgent({ approvals })
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  const newConversation = () => {
    abortRef.current?.abort()
    setHistory([])
    setItems([])
  }

  const suggestions = selectedFile && includeFile ? SUGGESTIONS_WITH_FILE : SUGGESTIONS

  return (
    <div className="agent-panel">
      <div className="agent-header">
        <Sparkles size={15} className="agent-header-icon" />
        <span className="agent-title">Assistant</span>
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
            <Sparkles size={28} />
            <p>Ask about your project, or have the assistant write models, tests and docs, then compile and validate them.</p>
            <div className="agent-suggestions">
              {suggestions.map(suggestion => (
                <button key={suggestion} className="agent-suggestion" onClick={() => sendMessage(suggestion)}>
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {items.map((item, index) => {
          switch (item.kind) {
            case 'user':
              return (
                <div key={index} className="agent-user-msg">
                  {item.context && (
                    <div className="agent-user-context">
                      <FileText size={11} /> {item.context}
                    </div>
                  )}
                  {item.text}
                </div>
              )
            case 'text':
              return item.text ? (
                <div key={index} className="agent-assistant-msg">
                  <ReactMarkdown>{item.text}</ReactMarkdown>
                </div>
              ) : null
            case 'thinking':
              return <ThinkingBlock key={index} text={item.text} />
            case 'tool':
              return <ToolCallCard key={item.id} item={item} onDecision={isRunning ? undefined : handleDecision} />
            case 'error':
              return (
                <div key={index} className="agent-error">
                  <AlertTriangle size={13} /> {item.text}
                </div>
              )
            case 'notice':
              return <div key={index} className="agent-notice">{item.text}</div>
          }
        })}

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
            placeholder="Ask the assistant…"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={3}
          />
          {isRunning ? (
            <button className="agent-send-btn" onClick={() => abortRef.current?.abort()} title="Stop">
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
