// Create, customize and delete agent teams. The built-in team is read-only and can be duplicated.

import { useState, useEffect } from 'react'
import { X, Users, Plus, Copy, Trash2, ChevronDown, ChevronRight, ShieldQuestion, Lock } from 'lucide-react'
import { apiUrl, apiFetch } from '../../config/api'
import { AgentTeam, TeamRole, TeamsInfo } from './agentTypes'
import './TeamEditorModal.css'

interface TeamEditorModalProps {
  projectPath: string
  info: TeamsInfo
  initialTeamId: string
  onClose: () => void
  // Called after a save or delete with the team to select
  onChanged: (selectTeamId: string) => void
}

const slugify = (text: string): string =>
  text.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').replace(/^(\d)/, 'r_$1').slice(0, 32) || 'role'

const emptyRole = (): TeamRole => ({
  id: '',
  label: '',
  description: '',
  instructions: '',
  tools: ['list_files', 'read_file', 'search_files', 'list_nodes', 'get_node'],
  model: '',
})

const copyTeam = (team: AgentTeam): AgentTeam => JSON.parse(JSON.stringify(team))

function RoleEditor({ role, index, info, readOnly, onChange, onRemove }: {
  role: TeamRole
  index: number
  info: TeamsInfo
  readOnly: boolean
  onChange: (role: TeamRole) => void
  onRemove: () => void
}) {
  const [open, setOpen] = useState(!role.label)
  const update = (patch: Partial<TeamRole>) => onChange({ ...role, ...patch })
  const toggleTool = (name: string) =>
    update({ tools: role.tools.includes(name) ? role.tools.filter(tool => tool !== name) : [...role.tools, name] })

  return (
    <div className="team-role">
      <div className="team-role-header">
        <button className="team-role-toggle" onClick={() => setOpen(!open)}>
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="team-role-name">{role.label || `Role ${index + 1}`}</span>
          <span className="team-role-meta">{role.tools.length} tools{role.model ? ` · ${role.model}` : ''}</span>
        </button>
        {!readOnly && (
          <button className="team-icon-btn" onClick={onRemove} title="Remove role">
            <Trash2 size={13} />
          </button>
        )}
      </div>
      {open && (
        <fieldset className="team-role-body" disabled={readOnly}>
          <div className="team-field-row">
            <label className="team-field">
              <span>Name</span>
              <input
                value={role.label}
                maxLength={40}
                placeholder="e.g. Performance auditor"
                onChange={e => update({ label: e.target.value, ...(role.id && role.id !== slugify(role.label) ? {} : { id: slugify(e.target.value) }) })}
              />
            </label>
            <label className="team-field team-field-small">
              <span>Id</span>
              <input value={role.id} maxLength={32} onChange={e => update({ id: e.target.value })} />
            </label>
            <label className="team-field team-field-small">
              <span>Model</span>
              <select value={role.model} onChange={e => update({ model: e.target.value })}>
                {info.models.map(model => (
                  <option key={model} value={model}>{model || `Default (${info.default_model})`}</option>
                ))}
              </select>
            </label>
          </div>
          <label className="team-field">
            <span>Description <em>shown to the lead to decide what to delegate</em></span>
            <input value={role.description} maxLength={200} onChange={e => update({ description: e.target.value })} />
          </label>
          <label className="team-field">
            <span>Instructions <em>the agent's system prompt for this role</em></span>
            <textarea rows={6} value={role.instructions} maxLength={6000} onChange={e => update({ instructions: e.target.value })} />
          </label>
          <div className="team-field">
            <span>Tools</span>
            <div className="team-tools">
              {info.tools.map(tool => (
                <label key={tool.name} className="team-tool" title={tool.description}>
                  <input type="checkbox" checked={role.tools.includes(tool.name)} onChange={() => toggleTool(tool.name)} />
                  <code>{tool.name}</code>
                  {tool.may_need_approval && <ShieldQuestion size={11} className="team-tool-approval" />}
                </label>
              ))}
            </div>
            <div className="team-hint"><ShieldQuestion size={11} /> can change files or the warehouse; asks for approval unless auto-approve is on</div>
          </div>
        </fieldset>
      )}
    </div>
  )
}

function TeamEditorModal({ projectPath, info, initialTeamId, onClose, onChanged }: TeamEditorModalProps) {
  const findTeam = (id: string) => info.teams.find(team => team.id === id) ?? info.teams[0]
  const [draft, setDraft] = useState<AgentTeam>(() => copyTeam(findTeam(initialTeamId)))
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const readOnly = Boolean(draft.builtin)
  const defaultTeam = info.teams.find(team => team.builtin)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const edit = (patch: Partial<AgentTeam>) => {
    setDraft(prev => ({ ...prev, ...patch }))
    setDirty(true)
  }

  const open = (team: AgentTeam) => {
    if (dirty && !window.confirm('Discard unsaved changes to this team?')) return
    setDraft(copyTeam(team))
    setDirty(false)
    setError(null)
  }

  // Copies the current draft, unsaved edits included
  const duplicate = () => {
    setDraft({ ...copyTeam(draft), id: '', builtin: false, name: `${draft.name} (copy)`.slice(0, 60) })
    setDirty(true)
    setError(null)
  }

  const newTeam = () => {
    open({ id: '', name: 'New team', description: '', workflow: '', roles: [emptyRole()], builtin: false })
    setDirty(true)
  }

  const request = async (endpoint: string, body: object) => {
    setSaving(true)
    setError(null)
    try {
      const response = await apiFetch(apiUrl(endpoint), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: projectPath, ...body }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        setError(data?.detail || `Request failed (${response.status})`)
        return null
      }
      return data
    } catch (err) {
      console.error('[TeamEditor] Request failed:', err)
      setError('Error connecting to backend')
      return null
    } finally {
      setSaving(false)
    }
  }

  const save = async () => {
    const data = await request('/api/agent/teams/save', { team: draft })
    if (data?.team) {
      setDraft(data.team)
      setDirty(false)
      onChanged(data.team.id)
    }
  }

  const remove = async () => {
    if (!draft.id || !window.confirm(`Delete the team "${draft.name}"?`)) return
    if (await request('/api/agent/teams/delete', { team_id: draft.id })) {
      onChanged(info.default_team_id)
      setDraft(copyTeam(findTeam(info.default_team_id)))
      setDirty(false)
    }
  }

  const updateRole = (index: number, role: TeamRole) => edit({ roles: draft.roles.map((r, i) => (i === index ? role : r)) })
  const addRole = (template?: TeamRole) => {
    const role = template ? { ...template, tools: [...template.tools] } : emptyRole()
    // Keep role ids unique when the same template is added twice
    let id = role.id
    for (let n = 2; id && draft.roles.some(r => r.id === id); n++) id = `${role.id}_${n}`.slice(0, 32)
    edit({ roles: [...draft.roles, { ...role, id }] })
  }

  return (
    <div className="team-modal-overlay" onClick={onClose}>
      <div className="team-modal" onClick={e => e.stopPropagation()}>
        <div className="team-modal-header">
          <Users size={16} />
          <h3>Agent teams</h3>
          <button className="team-icon-btn" onClick={onClose} title="Close"><X size={16} /></button>
        </div>

        <div className="team-modal-body">
          <div className="team-list">
            {info.teams.map(team => (
              <button key={team.id} className={`team-list-item ${team.id === draft.id ? 'active' : ''}`} onClick={() => open(team)}>
                <span className="team-list-name">{team.name}</span>
                <span className="team-list-meta">
                  {team.builtin && <><Lock size={10} /> built-in · </>}{team.roles.length} roles
                </span>
              </button>
            ))}
            {!draft.id && (
              <div className="team-list-item active">
                <span className="team-list-name">{draft.name || 'New team'}</span>
                <span className="team-list-meta">not saved</span>
              </div>
            )}
            <button className="team-btn team-list-new" onClick={newTeam}><Plus size={13} /> New team</button>
          </div>

          <div className="team-editor">
            {readOnly && (
              <div className="team-banner">
                <Lock size={13} />
                <span>The built-in team can't be edited. Duplicate it to adapt it to your needs.</span>
                <button className="team-btn team-btn-primary" onClick={duplicate}><Copy size={13} /> Duplicate</button>
              </div>
            )}
            <fieldset className="team-fields" disabled={readOnly}>
              <label className="team-field">
                <span>Team name</span>
                <input value={draft.name} maxLength={60} onChange={e => edit({ name: e.target.value })} />
              </label>
              <label className="team-field">
                <span>Description</span>
                <input value={draft.description} maxLength={300} onChange={e => edit({ description: e.target.value })} />
              </label>
              <label className="team-field">
                <span>Workflow <em>how the lead sequences the roles (e.g. tests first, then implementation, then review)</em></span>
                <textarea rows={4} value={draft.workflow} maxLength={4000} onChange={e => edit({ workflow: e.target.value })} />
              </label>
            </fieldset>

            <div className="team-roles-header">
              <span>Roles ({draft.roles.length}/8)</span>
              {!readOnly && draft.roles.length < 8 && (
                <div className="team-add-role">
                  <button className="team-btn" onClick={() => addRole()}><Plus size={13} /> Blank role</button>
                  {defaultTeam && (
                    <select
                      value=""
                      onChange={e => {
                        const template = defaultTeam.roles.find(role => role.id === e.target.value)
                        if (template) addRole(template)
                      }}
                    >
                      <option value="">Add from default team…</option>
                      {defaultTeam.roles.map(role => <option key={role.id} value={role.id}>{role.label}</option>)}
                    </select>
                  )}
                </div>
              )}
            </div>
            {draft.roles.map((role, index) => (
              <RoleEditor
                key={`${draft.id}-${index}`}
                role={role}
                index={index}
                info={info}
                readOnly={readOnly}
                onChange={updated => updateRole(index, updated)}
                onRemove={() => edit({ roles: draft.roles.filter((_, i) => i !== index) })}
              />
            ))}
          </div>
        </div>

        <div className="team-modal-footer">
          {error && <span className="team-error">{error}</span>}
          {!readOnly && draft.id && (
            <button className="team-btn team-btn-danger" onClick={remove} disabled={saving}><Trash2 size={13} /> Delete</button>
          )}
          <span className="team-footer-spacer" />
          {!readOnly && draft.id && (
            <button className="team-btn" onClick={duplicate} disabled={saving}><Copy size={13} /> Duplicate</button>
          )}
          <button className="team-btn" onClick={onClose}>Close</button>
          {!readOnly && (
            <button className="team-btn team-btn-primary" onClick={save} disabled={saving || !dirty}>
              {saving ? 'Saving…' : 'Save team'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default TeamEditorModal
