import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { HankTask } from '../../types/hankTasks';
import { formatCentralDateTime } from '../../utils/centralTime';
import { FormField } from '../ui/FormField';
import { HankTaskWorkflow } from './HankTaskWorkflow';
import { HankWatchWorkflow } from './HankWatchWorkflow';
import { getHankSessionScope, subscribeHankSession } from './hankSession';

const PAGE_SIZE = 20;
const FILTERS = [
  { value: 'all', label: 'All tasks' },
  { value: 'awaiting_review', label: 'Awaiting review' },
  { value: 'needs_attention', label: 'Needs attention' },
  { value: 'watching', label: 'Watching' },
  { value: 'snoozed', label: 'Snoozed' },
  { value: 'completed', label: 'Completed' },
  { value: 'cancelled', label: 'Cancelled' },
] as const;
type TaskFilter = (typeof FILTERS)[number]['value'];
type WorkspaceView = 'inbox' | 'new' | 'new_watch' | 'detail';

export function HankTaskWorkspace({
  taskId,
  onNavigate,
  onBusyChange,
}: {
  taskId?: number;
  onNavigate: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [scope] = useState(getHankSessionScope);
  const [sessionChanged, setSessionChanged] = useState(false);
  const [view, setView] = useState<WorkspaceView>(taskId ? 'detail' : 'inbox');
  const [selectedId, setSelectedId] = useState(taskId);
  const [task, setTask] = useState<HankTask>();
  const [detailLoading, setDetailLoading] = useState(!!taskId);
  const [detailError, setDetailError] = useState('');
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [newAttempt, setNewAttempt] = useState(0);
  const [writeBusy, setWriteBusy] = useState(false);
  const [filter, setFilter] = useState<TaskFilter>('all');
  const [tasks, setTasks] = useState<HankTask[]>([]);
  const [listLoading, setListLoading] = useState(!taskId);
  const [loadingMore, setLoadingMore] = useState(false);
  const [listError, setListError] = useState('');
  const [failedMore, setFailedMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [listAttempt, setListAttempt] = useState(0);
  const mounted = useRef(true);
  const busyRef = useRef(false);
  const lastTaskProp = useRef(taskId);
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const listGeneration = useRef(0);
  const currentSession = useCallback(() => mounted.current && getHankSessionScope() === scope, [scope]);

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeHankSession(() => {
      if (getHankSessionScope() !== scope) {
        listController.current?.abort();
        detailController.current?.abort();
        setSessionChanged(true);
      }
    });
    return () => {
      mounted.current = false;
      listController.current?.abort();
      detailController.current?.abort();
      unsubscribe();
    };
  }, [scope]);

  const handleBusy = useCallback(
    (busy: boolean) => {
      busyRef.current = busy;
      setWriteBusy(busy);
      onBusyChange(busy);
    },
    [onBusyChange]
  );

  // External task links wait for a pending write's receipt before replacing it.
  useEffect(() => {
    if (writeBusy || taskId === lastTaskProp.current) return;
    lastTaskProp.current = taskId;
    setSelectedId(taskId);
    setTask(undefined);
    setDetailError('');
    setDetailLoading(!!taskId);
    setView(taskId ? 'detail' : 'inbox');
  }, [taskId, writeBusy]);

  const loadPage = useCallback(
    async (append: boolean, cursor?: number) => {
      if (!currentSession() || (append && listController.current)) return;
      listController.current?.abort();
      const generation = ++listGeneration.current;
      const controller = new AbortController();
      listController.current = controller;
      setListLoading(true);
      setLoadingMore(append);
      setListError('');
      setFailedMore(false);
      try {
        const result = await api.getHankTasks(
          {
            limit: PAGE_SIZE,
            ...(cursor === undefined ? {} : { before_id: cursor }),
            ...(filter === 'all' ? {} : { status: filter }),
          },
          controller.signal
        );
        if (!currentSession() || controller.signal.aborted || generation !== listGeneration.current) return;
        setTasks(previous =>
          append
            ? Array.from(new Map([...previous, ...result.tasks].map(item => [item.id, item])).values())
            : result.tasks
        );
        setNextCursor(result.next_before_id);
        setHasMore(result.has_more);
      } catch {
        if (currentSession() && !controller.signal.aborted && generation === listGeneration.current) {
          setListError(append ? 'Older tasks could not be loaded.' : 'Your task inbox could not be loaded.');
          setFailedMore(append);
        }
      } finally {
        if (listController.current === controller) listController.current = null;
        if (currentSession() && !controller.signal.aborted && generation === listGeneration.current) {
          setListLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [currentSession, filter]
  );

  useEffect(() => {
    if (view !== 'inbox') return;
    void loadPage(false);
    return () => {
      listController.current?.abort();
      listController.current = null;
    };
  }, [view, listAttempt, loadPage]);

  useEffect(() => {
    if (view !== 'detail' || !selectedId) return;
    const controller = new AbortController();
    detailController.current = controller;
    setTask(undefined);
    setDetailError('');
    setDetailLoading(true);
    api
      .getHankTask(selectedId, controller.signal)
      .then(result => {
        if (currentSession() && !controller.signal.aborted) setTask(result);
      })
      .catch(() => {
        if (currentSession() && !controller.signal.aborted) {
          setDetailError('This task could not be loaded. It may belong to another session or no longer be available.');
        }
      })
      .finally(() => {
        if (detailController.current === controller) detailController.current = null;
        if (currentSession() && !controller.signal.aborted) setDetailLoading(false);
      });
    return () => {
      controller.abort();
    };
  }, [selectedId, detailAttempt, view, currentSession]);

  const openTask = (id: number) => {
    if (busyRef.current) return;
    setTask(undefined);
    setDetailError('');
    setDetailLoading(true);
    setSelectedId(id);
    setView('detail');
  };
  const newTask = (followUp = false) => {
    if (busyRef.current) return;
    setTask(undefined);
    setSelectedId(undefined);
    setNewAttempt(attempt => attempt + 1);
    setView(followUp ? 'new_watch' : 'new');
  };
  const taskChanged = (saved: HankTask) => {
    if (!currentSession()) return;
    setTask(saved);
    setTasks(previous => previous.map(item => (item.id === saved.id ? saved : item)));
    setListAttempt(attempt => attempt + 1);
  };

  if (sessionChanged || !currentSession()) {
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to see tasks for your current company.
      </p>
    );
  }

  return (
    <section aria-label="Your Hank tasks" className="space-y-4">
      <div className="space-y-3">
        <div>
          <h3 className="text-sm font-semibold text-fd-ink">Your tasks</h3>
          <p className="mt-1 text-xs text-fd-mute">
            Your saved proposals, follow-ups, and receipts in this company. Review a proposal to confirm its action.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => newTask()} disabled={writeBusy} className="btn text-xs">
            New task
          </button>
          <button type="button" onClick={() => newTask(true)} disabled={writeBusy} className="btn text-xs">
            New follow-up
          </button>
        </div>
      </div>
      {view !== 'inbox' && (
        <button
          type="button"
          disabled={writeBusy}
          className="text-xs text-fd-blue underline"
          onClick={() => {
            if (!busyRef.current) setView('inbox');
          }}
        >
          Back to task inbox
        </button>
      )}

      {view === 'inbox' ? (
        <>
          <div className="flex items-end gap-2">
            <FormField label="Task status" className="min-w-0 flex-1">
              {field => (
                <select
                  {...field}
                  className="input w-full"
                  value={filter}
                  disabled={writeBusy}
                  onChange={event => {
                    listController.current?.abort();
                    setTasks([]);
                    setNextCursor(null);
                    setHasMore(false);
                    setListLoading(true);
                    setFilter(event.target.value as TaskFilter);
                  }}
                >
                  {FILTERS.map(option => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <button
              type="button"
              disabled={listLoading || writeBusy}
              className="btn text-xs"
              onClick={() => setListAttempt(attempt => attempt + 1)}
            >
              Refresh inbox
            </button>
          </div>
          {listLoading && (
            <p role="status" className="text-xs text-fd-mute">
              {loadingMore ? 'Loading older tasks…' : 'Loading your task inbox…'}
            </p>
          )}
          {listError && (
            <div role="alert" className="text-xs text-fd-red space-y-1">
              <p>{listError}</p>
              {!!tasks.length && <p className="text-fd-mute">Showing the tasks already loaded.</p>}
              <button
                type="button"
                disabled={listLoading}
                className="underline"
                onClick={() => {
                  if (failedMore && nextCursor !== null) void loadPage(true, nextCursor);
                  else setListAttempt(attempt => attempt + 1);
                }}
              >
                Retry loading tasks
              </button>
            </div>
          )}
          {!listLoading && !listError && !tasks.length && (
            <p className="text-sm text-fd-mute">
              {filter === 'all'
                ? 'No saved tasks yet. Start a new task to prepare a proposal.'
                : `No ${FILTERS.find(option => option.value === filter)?.label.toLowerCase()} tasks.`}
            </p>
          )}
          <div className="space-y-2">
            {tasks.map(item => (
              <article key={item.id} className="rounded-[3px] border border-slate-700 p-3 space-y-2">
                <button
                  type="button"
                  disabled={writeBusy}
                  onClick={() => openTask(item.id)}
                  className="block text-left w-full space-y-1"
                >
                  <span className="block text-sm font-semibold text-fd-blue">{item.title}</span>
                  <span className="block text-[11px] text-fd-mute">
                    {item.status.replace(/_/g, ' ')} · Updated {formatCentralDateTime(item.updated_at)}
                  </span>
                  <span className="block text-xs text-fd-body line-clamp-2">
                    {item.status === 'completed' && item.result ? item.result.summary : item.preview.summary}
                  </span>
                </button>
                {item.error_message && <p className="text-xs text-fd-amber">{item.error_message}</p>}
                {item.kind === 'watch_work_order' && (
                  <p className="text-xs text-fd-mute">
                    {item.last_checked_at
                      ? `Last checked ${formatCentralDateTime(item.last_checked_at)}`
                      : ['watching', 'snoozed'].includes(item.status)
                        ? 'Waiting for first check'
                        : 'Not checked'}
                  </p>
                )}
                {item.status === 'completed' && !!item.result?.references.length && (
                  <div className="flex flex-wrap gap-2">
                    {item.result.references.map(reference => (
                      <Link
                        key={`${reference.type}:${reference.id}`}
                        to={reference.url}
                        onClick={onNavigate}
                        className="text-xs text-fd-blue underline"
                      >
                        {reference.label}
                      </Link>
                    ))}
                  </div>
                )}
              </article>
            ))}
          </div>
          {hasMore && nextCursor !== null && (
            <button
              type="button"
              disabled={listLoading || writeBusy}
              className="btn text-xs"
              onClick={() => void loadPage(true, nextCursor)}
            >
              Load older tasks
            </button>
          )}
          {!!tasks.length && (
            <p className="text-[11px] text-fd-mute">
              Showing {tasks.length} loaded task{tasks.length === 1 ? '' : 's'}
              {hasMore ? '; older tasks are available.' : '.'} Refresh to check for changes.
            </p>
          )}
        </>
      ) : detailLoading && view === 'detail' ? (
        <p role="status" className="text-sm text-fd-mute">
          Loading your task…
        </p>
      ) : detailError && view === 'detail' ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-fd-red">
            {detailError}
          </p>
          <button type="button" className="btn text-xs" onClick={() => setDetailAttempt(attempt => attempt + 1)}>
            Retry task
          </button>
        </div>
      ) : view === 'new_watch' || task?.kind === 'watch_work_order' ? (
        <HankWatchWorkflow
          key={view === 'new_watch' ? `watch:${newAttempt}` : `${selectedId}:${detailAttempt}`}
          initialTask={task}
          onNavigate={onNavigate}
          onBusyChange={handleBusy}
          onTaskChanged={taskChanged}
        />
      ) : (
        <HankTaskWorkflow
          key={view === 'new' ? `new:${newAttempt}` : `${selectedId}:${detailAttempt}`}
          initialTask={task}
          onNavigate={onNavigate}
          onBusyChange={handleBusy}
          onTaskChanged={taskChanged}
        />
      )}
    </section>
  );
}
