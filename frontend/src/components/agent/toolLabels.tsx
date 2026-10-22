// Human-readable labels for agent tool calls

import { FileText, FileEdit, FolderTree, Search, Network, Play, Database, Code, Users } from 'lucide-react'

export const str = (value: unknown): string => (typeof value === 'string' ? value : '')

export function describeTool(name: string, input: Record<string, unknown> = {}): { icon: JSX.Element; label: string } {
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
    case 'run_subagents':
      return { icon: <Users size={13} />, label: 'Dispatch agents' }
    default:
      return { icon: <Code size={13} />, label: name }
  }
}
