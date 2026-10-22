// Renders a conversation (or a sub-agent's activity): messages, thinking, tool calls and team runs

import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  Brain, ChevronRight, ChevronDown, FileText, AlertTriangle, Loader2, Check, X, Users,
  FlaskConical, Code2, Eye, BookOpen, Bot,
} from 'lucide-react'
import { AgentRun, DisplayItem, ToolItem } from './agentTypes'
import ToolCallCard from './ToolCallCard'
import { describeTool } from './toolLabels'

export type DecisionHandler = (toolId: string, approved: boolean) => void

const ROLE_ICONS: Record<string, JSX.Element> = {
  tester: <FlaskConical size={13} />,
  developer: <Code2 size={13} />,
  reviewer: <Eye size={13} />,
  documenter: <BookOpen size={13} />,
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

// One-line description of what a sub-agent is doing now
function latestActivity(agent: AgentRun): string {
  if (agent.status !== 'running') return agent.summary?.split('\n')[0] ?? ''
  for (let i = agent.items.length - 1; i >= 0; i--) {
    const item = agent.items[i]
    if (item.kind === 'tool') {
      const label = describeTool(item.name, item.input).label
      return item.status === 'awaiting' ? `Waiting for approval: ${label}` : label
    }
    if (item.kind === 'text' && item.text.trim()) return item.text.trim().split('\n')[0]
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
        <span className={`agent-role-badge agent-role-${agent.role}`}>
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
        <span>{agents.length > 0 ? `${agents.length} agent${agents.length > 1 ? 's' : ''} working in parallel` : 'Dispatching agents…'}</span>
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
                <ReactMarkdown>{item.text}</ReactMarkdown>
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
