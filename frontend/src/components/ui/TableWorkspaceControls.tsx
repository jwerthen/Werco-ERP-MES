import React, { useId, useState } from 'react';
import { TableWorkspace } from '../../hooks/useTableWorkspace';

const workspaceButton = 'btn-secondary btn-sm enabled:border-slate-500 enabled:bg-slate-800 enabled:text-slate-100 enabled:hover:border-sky-300 enabled:hover:bg-slate-700 focus-visible:ring-sky-300 disabled:border-slate-600 disabled:text-slate-400 disabled:opacity-100';

export function TableWorkspaceControls<T>({
  workspace: w,
  tableOptions = true,
}: {
  workspace: TableWorkspace<T>;
  tableOptions?: boolean;
}) {
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [visibility, setVisibility] = useState<'private' | 'team'>('private');
  const [removing, setRemoving] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const id = useId();
  const locked = w.busy || w.loading || !w.enabled;
  const hasSelection = w.views.some(row => row.key === selected);
  const lockedReason = w.busy ? 'Saving a workspace change. Please wait.' : w.loading ? 'Loading your saved views…' : !w.enabled ? 'Sign in to save or apply views.' : '';
  const applyReason = lockedReason || (!hasSelection ? 'Select a saved view to apply it.' : '');
  const saveReason = lockedReason || (!name.trim() ? 'Enter a view name to save.' : '');
  const label = (key: string) => {
    const column = w.columns.find(item => item.key === key);
    return typeof column?.header === 'string' && column.header.trim()
      ? column.header
      : key.replace(/_/g, ' ').replace(/^./, letter => letter.toUpperCase());
  };
  return (
    <div className="mb-3 min-w-0 border border-fd-line bg-surface-50 p-3 text-sm" aria-label="Saved workspace">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Saved views"
          className="input w-auto max-w-full"
          disabled={locked}
          aria-describedby={locked ? `${id}-availability` : undefined}
          title={lockedReason || undefined}
          value={hasSelection ? selected : ''}
          onChange={event => {
            setSelected(event.target.value);
            setRemoving(false);
          }}
        >
          <option value="">Saved views</option>
          {w.views.map(row => (
            <option key={row.key} value={row.key}>
              {row.name}
              {row.visibility === 'team' ? ' · Team' : ' · Private'}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={workspaceButton}
          disabled={locked || !hasSelection}
          aria-describedby={applyReason ? `${id}-availability` : undefined}
          title={applyReason || undefined}
          onClick={() => w.apply(selected)}
        >
          Apply view
        </button>
        {tableOptions && (
          <button
            type="button"
            className={workspaceButton}
            aria-expanded={optionsOpen}
            aria-controls={`${id}-options`}
            onClick={() => setOptionsOpen(value => !value)}
          >
            Table options
          </button>
        )}
        <button
          type="button"
          className={workspaceButton}
          aria-expanded={saveOpen}
          aria-controls={`${id}-save`}
          onClick={() => setSaveOpen(value => !value)}
        >
          Save current view
        </button>
        {hasSelection && w.canEditView(selected) && (
          <>
            <button
              type="button"
              className={workspaceButton}
              disabled={locked}
              aria-describedby={locked ? `${id}-availability` : undefined}
              title={lockedReason || undefined}
              onClick={() => void w.updateView(selected)}
            >
              Update view
            </button>
            <button type="button" className={workspaceButton} disabled={locked} aria-describedby={locked ? `${id}-availability` : undefined} title={lockedReason || undefined} onClick={() => setRemoving(true)}>
              Remove view
            </button>
          </>
        )}
      </div>
      {applyReason && <p id={`${id}-availability`} className="mt-2 text-xs text-slate-300" role={w.loading || w.busy ? 'status' : undefined}>{applyReason}</p>}
      {optionsOpen && (
        <div id={`${id}-options`} className="mt-3 space-y-3 border-t border-fd-line pt-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={w.layout.dense}
                onChange={event => w.change({ ...w.layout, dense: event.target.checked })}
              />{' '}
              Compact rows
            </label>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={workspaceButton}
                disabled={locked}
                aria-describedby={locked ? `${id}-availability` : undefined}
                title={lockedReason || undefined}
                onClick={() => void w.saveLayout()}
              >
                Save layout
              </button>
              <button type="button" className={workspaceButton} onClick={w.reset}>
                Reset layout
              </button>
            </div>
          </div>
          <p className="text-xs text-slate-300">
            Choose and order desktop columns. Record identity and actions stay visible.
          </p>
          <ol className="grid grid-cols-1 gap-x-6 gap-y-2 md:grid-cols-2 xl:grid-cols-3">
            {w.layout.order.map((key, index) => {
              const required = key === w.columns[0]?.key || /action/i.test(key);
              const move = (direction: number) => {
                const order = [...w.layout.order];
                [order[index], order[index + direction]] = [order[index + direction], order[index]];
                w.change({ ...w.layout, order });
              };
              return (
                <li key={key} className="flex min-w-0 items-center gap-2">
                  <label className="flex min-w-0 flex-1 items-center gap-2">
                    <input
                      type="checkbox"
                      disabled={required}
                      title={required ? 'Required column: always visible.' : undefined}
                      checked={!w.layout.hidden.includes(key)}
                      onChange={event =>
                        w.change({
                          ...w.layout,
                          hidden: event.target.checked
                            ? w.layout.hidden.filter(item => item !== key)
                            : [...w.layout.hidden, key],
                        })
                      }
                    />
                    <span className="break-words">{label(key)}</span>
                  </label>
                  <button
                    type="button"
                    className={workspaceButton}
                    aria-label={`Move ${label(key)} up`}
                    disabled={index === 0}
                    title={index === 0 ? 'Already the first column.' : undefined}
                    onClick={() => move(-1)}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className={workspaceButton}
                    aria-label={`Move ${label(key)} down`}
                    disabled={index === w.layout.order.length - 1}
                    title={index === w.layout.order.length - 1 ? 'Already the last column.' : undefined}
                    onClick={() => move(1)}
                  >
                    ↓
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      )}
      {saveOpen && (
        <form
          id={`${id}-save`}
          className="mt-3 flex flex-wrap gap-2 border-t border-fd-line pt-3"
          onSubmit={event => {
            event.preventDefault();
            if (name.trim()) void w.saveView(name.trim(), w.canManageTeam ? visibility : 'private');
          }}
        >
          <input
            aria-label="View name"
            value={name}
            onChange={event => setName(event.target.value)}
            maxLength={100}
            className="input w-auto max-w-full"
            placeholder="e.g. Open priority jobs"
            required
          />
          {w.canManageTeam && (
            <select
              aria-label="View visibility"
              className="input w-auto max-w-full"
              value={visibility}
              onChange={event => setVisibility(event.target.value as 'private' | 'team')}
            >
              <option value="private">Private — only me</option>
              <option value="team">Team — permitted colleagues</option>
            </select>
          )}
          <button className="btn-primary btn-sm" disabled={locked || !name.trim()} aria-describedby={saveReason ? `${id}-save-reason` : undefined} title={saveReason || undefined}>
            Save view
          </button>
          {saveReason && <p id={`${id}-save-reason`} className="w-full text-xs text-slate-300">{saveReason}</p>}
          <span className="w-full text-xs text-slate-300">
            {tableOptions
              ? 'Saves filters, sort, columns and row density.'
              : 'Saves the current filters and view settings.'}{' '}
            Team views are available to colleagues with access to this module; only managers can change them. Drafts
            stay private.
          </span>
        </form>
      )}
      {removing && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          Remove this saved view?
          <button
            type="button"
            className="btn-danger btn-sm"
            disabled={locked}
            aria-describedby={locked ? `${id}-availability` : undefined}
            title={lockedReason || undefined}
            onClick={() => {
              void w.remove(selected);
              setRemoving(false);
            }}
          >
            Confirm removal
          </button>
          <button type="button" className={workspaceButton} onClick={() => setRemoving(false)}>
            Keep view
          </button>
        </div>
      )}
      {w.message && (
        <p className="mt-2 text-xs" role="status">
          {w.message}
        </p>
      )}
      {w.error && (
        <p className="mt-2 text-red-300" role="alert">
          {w.error}{' '}
          <button type="button" className="underline" disabled={w.busy} aria-describedby={w.busy ? `${id}-availability` : undefined} title={w.busy ? lockedReason : undefined} onClick={() => void w.reload()}>
            Reload saved views
          </button>
        </p>
      )}
    </div>
  );
}
