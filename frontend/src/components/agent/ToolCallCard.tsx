// Renders one tool call made by the assistant, with its approval controls and result

import { useState } from 'react'
import {
  ChevronRight, ChevronDown, FileText, FileEdit, FolderTree, Search, Network,
  Play, Database, Code, Loader2, Check, X, AlertTriangle, ShieldQuestion,
} from 'lucide-react'
import { ToolItem } from './agentTypes'

interface ToolCallCardProps {
  item: ToolItem
  onDecision?: (toolId: string, approved: boolean) => void
}

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

function describeTool(name: string, input: Record<string, unknown> = {}): { icon: JSX.Element; label: string } {
  switch (name) {
    case 'list_files':
      return { icon: <FolderTree size={13} />, label: `List files ${str(input.directory) || '/'}` }
    case 'read_file':
      return { icon: <FileText size={13} />, label: `Read ${str(input.path)}` }
    case 'search_files':
      return { icon: <Search size={13} />, label: `Search "${str(input.pattern)}"${input.glob ? ` in ${str(input.glob)}` : ''}` }
    case 'write_file':
      return { icon: <FileEdit size={13} />, label: `Write ${str(input.path)}` }
    case 'edit_file':
      return { icon: <FileEdit size={13} />, label: `Edit ${str(input.path)}` }
    case 'list_nodes':
      return { icon: <Network size={13} />, label: `List ${str(input.resource_type) || 'nodes'}${input.name_contains ? ` matching "${str(input.name_contains)}"` : ''}` }
    case 'get_node':
      return { icon: <Network size={13} />, label: `Inspect ${str(input.name)}` }
    case 'get_compiled_sql':
      return { icon: <Code size={13} />, label: `Compiled SQL of ${str(input.name)}` }
    case 'run_dbt': {
      const select = str(input.select)
      return {
        icon: <Play size={13} />,
        label: `dbt ${str(input.command)}${select ? ` --select ${select}` : ''}${input.full_refresh ? ' --full-refresh' : ''}`,
      }
    }
    case 'run_query':
      return { icon: <Database size={13} />, label: 'Run query' }
    default:
      return { icon: <Code size={13} />, label: name }
  }
}

function StatusIcon({ status }: { status: ToolItem['status'] }) {
  switch (status) {
    case 'streaming':
    case 'queued':
    case 'running':
      return <Loader2 size={13} className="agent-spin" />
    case 'awaiting':
      return <ShieldQuestion size={13} className="agent-status-awaiting" />
    case 'done':
      return <Check size={13} className="agent-status-done" />
    case 'declined':
      return <X size={13} className="agent-status-declined" />
    case 'error':
      return <AlertTriangle size={13} className="agent-status-error" />
  }
}

// What the user is asked to approve, shown before the call runs
function ApprovalPreview({ name, input }: { name: string; input: Record<string, unknown> }) {
  if (name === 'edit_file') {
    return (
      <div className="agent-diff">
        <pre className="agent-diff-old">{str(input.old_str)}</pre>
        <pre className="agent-diff-new">{str(input.new_str)}</pre>
      </div>
    )
  }
  if (name === 'write_file') {
    return <pre className="agent-tool-pre agent-diff-new">{str(input.content)}</pre>
  }
  if (name === 'run_dbt') {
    return <div className="agent-approval-note">This command materializes data in the warehouse.</div>
  }
  return <pre className="agent-tool-pre">{JSON.stringify(input, null, 2)}</pre>
}

function ToolCallCard({ item, onDecision }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false)
  const { icon, label } = describeTool(item.name, item.input)
  const awaiting = item.status === 'awaiting'
  const showDetails = expanded || awaiting

  return (
    <div className={`agent-tool agent-tool-${item.status}`}>
      <button className="agent-tool-header" onClick={() => setExpanded(!expanded)} disabled={awaiting}>
        {showDetails ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {icon}
        <span className="agent-tool-label" title={label}>{label}</span>
        <StatusIcon status={item.status} />
      </button>

      {showDetails && (
        <div className="agent-tool-body">
          {awaiting && item.input && <ApprovalPreview name={item.name} input={item.input} />}
          {!awaiting && item.name === 'run_query' && item.input && (
            <pre className="agent-tool-pre">{str(item.input.sql)}</pre>
          )}
          {!awaiting && (item.name === 'edit_file' || item.name === 'write_file') && item.input && (
            <ApprovalPreview name={item.name} input={item.input} />
          )}
          {item.result !== undefined && <pre className="agent-tool-pre agent-tool-result">{item.result}</pre>}
        </div>
      )}

      {awaiting && onDecision && (
        <div className="agent-approval-actions">
          {item.decision === undefined ? (
            <>
              <button className="agent-btn agent-btn-primary" onClick={() => onDecision(item.id, true)}>
                <Check size={13} /> Approve
              </button>
              <button className="agent-btn" onClick={() => onDecision(item.id, false)}>
                <X size={13} /> Decline
              </button>
            </>
          ) : (
            <span className="agent-approval-note">{item.decision ? 'Approved' : 'Declined'}, waiting for other decisions…</span>
          )}
        </div>
      )}
    </div>
  )
}

export default ToolCallCard
