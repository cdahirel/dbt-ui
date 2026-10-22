// Renders a conversation (or a sub-agent's activity): messages, thinking, tool calls and team runs

import { useState } from 'react'
import ReactMarkdown, { Components, Options } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Brain, ChevronRight, ChevronDown, FileText, AlertTriangle, Loader2, Check, X, Users,
  FlaskConical, Code2, Eye, BookOpen, Bot,
} from 'lucide-react'
import { AgentRun, DisplayItem, ToolItem } from './agentTypes'
import ToolCallCard from './ToolCallCard'
import { describeTool } from './toolLabels'

export type DecisionHandler = (toolId: string, approved: boolean) => void

// Single tildes are common in prose ("~2 min") and must not become strikethrough
const REMARK_PLUGINS: Options['remarkPlugins'] = [[remarkGfm, { singleTilde: false }]]

const MARKDOWN_COMPONENTS: Components = {
  // Links open in a new tab so the app (and unsaved work) stays open
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
  // Wide tables scroll inside a wrapper, keeping native table semantics
  table: ({ node: _node, ...props }) => (
    <div className="agent-table-wrapper">
      <table {...props} />
    </div>
  ),
}

const ROLE_ICONS: Record<string, JSX.Element> = {
  tester: <FlaskConical size={13} />,
  developer: <Code2 size={13} />,
  reviewer: <Eye size={13} />,
  documenter: <BookOpen size={13} />,
}

const KNOWN_ROLES = new Set(Object.keys(ROLE_ICONS))

// Default roles have fixed colors; custom roles get a stable color from their id
function roleColorClass(role: string): string {
  if (KNOWN_ROLES.has(role)) return `agent-role-${role}`
  const hash = [...role].reduce((acc, char) => (acc * 31 + char.charCodeAt(0)) >>> 0, 0)
  return `agent-role-c${hash % 5}`
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

// First line of Markdown text that reads as prose (skips table rows and rules)
function firstLine(text: string): string {
  return text.split('\n').map(line => line.trim()).find(line => line && !/^(\||-{3,}|`{3})/.test(line)) ?? ''
}

// One-line description of what a sub-agent is doing now
function latestActivity(agent: AgentRun): string {
  if (agent.status !== 'running') return firstLine(agent.summary ?? '')
  for (let i = agent.items.length - 1; i >= 0; i--) {
    const item = agent.items[i]
    if (item.kind === 'tool') {
      const label = describeTool(item.name, item.input).label
      return item.status === 'awaiting' ? `Waiting for approval: ${label}` : label
    }
    if (item.kind === 'text' && firstLine(item.text)) return firstLine(item.text)
    if (item.kind === 'thinking') return 'Thinking…'
  }
  return 'Starting…'
}

function AgentLane({ agent, onDecision }: { agent: AgentRun; onDecision?: DecisionHandler }) {
  const [open, setOpen] = useState(false)
  const awaiting = agent.items.some(item => item.kind === 'tool' && item.status === 'awaiting')
  const expanded = open || awaiting

  return (
    <div className={`agent-lane agent-lane-${agent.status} ${awaiting ? 'agent-lane-awaiting' : ''}`}>
      <button className="agent-lane-header" onClick={() => setOpen(!open)}>
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <span className={`agent-role-badge ${roleColorClass(agent.role)}`}>
          {ROLE_ICONS[agent.role] ?? <Bot size={13} />} {agent.label}
        </span>
        <span className="agent-lane-title" title={agent.title}>{agent.title}</span>
        {agent.status === 'running' && <Loader2 size={13} className="agent-spin" />}
        {agent.status === 'done' && <Check size={13} className="agent-status-done" />}
        {agent.status === 'error' && <AlertTriangle size={13} className="agent-status-error" />}
        {agent.status === 'cancelled' && <X size={13} className="agent-status-declined" />}
      </button>
      {!expanded && <div className="agent-lane-activity">{latestActivity(agent)}</div>}
      {expanded && (
        <div className="agent-lane-body">
          <AgentItems items={agent.items} onDecision={onDecision} />
          {agent.status === 'error' && agent.summary && <div className="agent-error"><AlertTriangle size={13} /> {agent.summary}</div>}
        </div>
      )}
    </div>
  )
}

function TeamRunCard({ item, agents, onDecision }: { item: ToolItem; agents: AgentRun[]; onDecision?: DecisionHandler }) {
  const finished = agents.filter(agent => agent.status !== 'running').length
  return (
    <div className="agent-team-run">
      <div className="agent-team-run-header">
        <Users size={13} />
        <span>
          {agents.length === 0
            ? 'Dispatching agents…'
            : `${agents.length} agent${agents.length > 1 ? 's' : ''} ${finished === agents.length ? 'finished' : 'working'}`}
        </span>
        {agents.length > 0 && <span className="agent-team-run-count">{finished}/{agents.length}</span>}
      </div>
      {agents.map(agent => <AgentLane key={agent.id} agent={agent} onDecision={onDecision} />)}
      {item.status === 'error' && item.result && <div className="agent-error"><AlertTriangle size={13} /> {item.result}</div>}
    </div>
  )
}

interface AgentItemsProps {
  items: DisplayItem[]
  agents?: AgentRun[]
  onDecision?: DecisionHandler
}

function AgentItems({ items, agents = [], onDecision }: AgentItemsProps) {
  return (
    <>
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
                <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={MARKDOWN_COMPONENTS}>{item.text}</ReactMarkdown>
              </div>
            ) : null
          case 'thinking':
            return <ThinkingBlock key={index} text={item.text} />
          case 'tool':
            return item.name === 'run_subagents' ? (
              <TeamRunCard key={item.id} item={item} agents={agents.filter(agent => agent.parentToolId === item.id)} onDecision={onDecision} />
            ) : (
              <ToolCallCard key={item.id} item={item} onDecision={onDecision} />
            )
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
    </>
  )
}

export default AgentItems
