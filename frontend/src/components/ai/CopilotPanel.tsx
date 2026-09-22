/**
 * Hank — shop teammate with ERP lookups and employee-reviewed tasks.
 *
 * - Opens from the header Hank button or Ctrl+. (wired in Layout).
 * - Conversation history is CLIENT-held, memory only: it survives open/close
 *   (the component stays mounted) but resets on reload. Nothing is persisted.
 * - Answers stream over SSE (api.copilotChatStream); tool activity renders as
 *   a hint line while the model is looking things up, and entity references
 *   come back as router deep links.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  ArrowPathIcon,
  ArrowUpTrayIcon,
  PaperAirplaneIcon,
  StopIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { CopilotMessage, CopilotReference, CopilotToolTraceEntry } from '../../types/copilot';
import { usePermissions } from '../../hooks/usePermissions';
import { canPublishDocuments } from '../../utils/recordWriteAccess';
import { HankAvatar } from './HankAvatar';
import { HankDocumentUpload } from './HankDocumentUpload';
import { HankBriefing } from './HankBriefing';
import { HankTaskWorkspace } from './HankTaskWorkspace';
import { HankPreferences } from './HankPreferences';
import { HankWorkWorkspace, HankWorkArea } from './HankWorkWorkspace';
import { HankVoiceInput } from './HankVoiceInput';
import { hankRecordContext } from './hankContext';
import { getHankSessionScope, isHankReadOnlySession } from './hankSession';

export interface CopilotPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

interface ChatEntry {
  role: 'user' | 'assistant';
  content: string;
  references?: CopilotReference[];
  toolTrace?: CopilotToolTraceEntry[];
  truncated?: boolean;
  error?: boolean;
}

const MAX_HISTORY_SENT = 40; // matches the backend request schema cap
const MAX_DISPLAYED_ENTRIES = 200; // render cap — very long sessions stay responsive
interface Suggestion {
  label: string;
  prompt: string;
  draft?: boolean;
}

const SHOP_SUGGESTIONS: Suggestion[] = [
  {
    label: 'Brief me on the shop',
    prompt:
      'Give me a shop briefing: active jobs, open blockers, and schedule conflicts. Link to the records I should review.',
  },
  { label: "What's blocked right now?", prompt: "What's blocked right now?" },
  { label: 'How loaded is the laser this week?', prompt: 'How loaded is the laser this week?' },
];

function toApiMessages(entries: ChatEntry[]): CopilotMessage[] {
  return entries
    .filter(entry => !entry.error && entry.content.trim().length > 0)
    .map(entry => ({ role: entry.role, content: entry.content }))
    .slice(-MAX_HISTORY_SENT);
}

export function CopilotPanel({ isOpen, onClose }: CopilotPanelProps) {
  const location = useLocation();
  const { role, isSuperuser } = usePermissions();
  const canUpload = canPublishDocuments({ role, is_superuser: isSuperuser }) && !isHankReadOnlySession();
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [activity, setActivity] = useState<string | null>(null);
  const [streamText, setStreamText] = useState('');
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [view, setView] = useState<'chat' | 'briefing' | 'tasks' | 'work' | 'preferences'>('chat');
  const [taskBusy, setTaskBusy] = useState(false);
  const [preferencesBusy, setPreferencesBusy] = useState(false);
  const [workBusy, setWorkBusy] = useState(false);
  const [workSelection, setWorkSelection] = useState<{ area: HankWorkArea; id?: number }>({ area: 'overview' });
  const [workNavigation, setWorkNavigation] = useState('initial');
  const recordContext = useMemo(
    () => hankRecordContext(location.pathname, location.search),
    [location.pathname, location.search]
  );
  const panelRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const turnGenerationRef = useRef(0);
  const sessionScopeRef = useRef(getHankSessionScope());
  // Mirror of `entries` so send/retry can compute the next history without
  // putting side effects inside a state updater (StrictMode double-invokes those).
  const entriesRef = useRef<ChatEntry[]>(entries);
  useEffect(() => {
    entriesRef.current = entries;
  }, [entries]);

  const contextHint = useMemo(() => `viewing ${location.pathname}${location.search}`.slice(0, 500), [location]);
  const taskId = useMemo(() => {
    const value = Number(new URLSearchParams(location.search).get('hank_task'));
    return Number.isSafeInteger(value) && value > 0 ? value : undefined;
  }, [location.search]);
  const workLink = useMemo(() => {
    const params = new URLSearchParams(location.search);
    const area = params.get('hank_work');
    const id = Number(params.get('hank_id'));
    return area && ['handoff', 'routine', 'intake'].includes(area) && Number.isSafeInteger(id) && id > 0
      ? { area: area as HankWorkArea, id }
      : undefined;
  }, [location.search]);
  const handledTaskNavigation = useRef<string | undefined>(undefined);
  const [taskNavigation, setTaskNavigation] = useState('initial');
  useEffect(() => {
    const navigation = `${location.key}:${taskId ?? ''}:${workLink?.area ?? ''}:${workLink?.id ?? ''}`;
    if (navigation === handledTaskNavigation.current || busy || taskBusy || workBusy || preferencesBusy || uploadBusy)
      return;
    handledTaskNavigation.current = navigation;
    if (workLink) {
      setUploadOpen(false);
      setWorkSelection(workLink);
      setWorkNavigation(navigation);
      setView('work');
    } else if (taskId) {
      setUploadOpen(false);
      setTaskNavigation(navigation);
      setView('tasks');
    }
  }, [taskId, workLink, location.key, busy, taskBusy, workBusy, preferencesBusy, uploadBusy]);
  const workOrderId = useMemo(() => {
    const match = /^\/work-orders\/(\d+)$/.exec(location.pathname);
    const id = match ? Number(match[1]) : undefined;
    return id !== undefined && Number.isSafeInteger(id) && id > 0 ? id : undefined;
  }, [location.pathname]);
  const suggestions = useMemo(() => {
    if (workOrderId !== undefined) {
      return [
        {
          label: 'Brief me on this job',
          prompt: `Look up work order ID ${workOrderId}. Summarize its status, operations, blockers, and recent activity, with a link to the job.`,
        },
        {
          label: 'What is holding this job up?',
          prompt: `Look up work order ID ${workOrderId} and explain its open blockers and current operation. Use the recorded facts.`,
        },
        SHOP_SUGGESTIONS[0],
      ];
    }
    if (/^\/(inventory|parts)(\/|$)/.test(location.pathname)) {
      return [
        {
          label: 'Check stock for a part…',
          prompt: 'Check available inventory, locations, and lots for part ',
          draft: true,
        },
        { label: 'Find a part…', prompt: 'Find part ', draft: true },
        SHOP_SUGGESTIONS[0],
      ];
    }
    if (/^\/(customers|purchasing)(\/|$)/.test(location.pathname)) {
      return [
        {
          label: 'Look up customer orders…',
          prompt: 'Show open work orders and active quotes for customer ',
          draft: true,
        },
        { label: 'Find a purchase order…', prompt: 'Find purchase order ', draft: true },
        SHOP_SUGGESTIONS[0],
      ];
    }
    return SHOP_SUGGESTIONS;
  }, [location.pathname, workOrderId]);

  const replaceEntries = useCallback((next: ChatEntry[]) => {
    entriesRef.current = next;
    setEntries(next);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    const timer = window.setTimeout(() => inputRef.current?.focus(), 150);
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const controls = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(
          'a[href],button:not(:disabled),input:not(:disabled):not([type="hidden"]),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]'
        ) ?? []
      );
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || !panelRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last?.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last || !panelRef.current?.contains(document.activeElement))
      ) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener('keydown', keydown);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('keydown', keydown);
      if (previous && document.contains(previous)) previous.focus();
    };
  }, [isOpen, onClose]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && typeof el.scrollTo === 'function') {
      el.scrollTo({ top: uploadOpen || view !== 'chat' ? 0 : el.scrollHeight });
    }
  }, [entries, streamText, activity, isOpen, uploadOpen, view]);

  useEffect(() => {
    if (!isOpen) return;
    if (uploadOpen) {
      const target =
        panelRef.current?.querySelector<HTMLElement>('input:not(:disabled)') ??
        panelRef.current?.querySelector<HTMLElement>('button[aria-label="Close Hank"]');
      target?.focus();
    } else if (view !== 'chat') {
      scrollRef.current?.querySelector<HTMLElement>('button:not(:disabled),a[href]')?.focus();
    } else {
      inputRef.current?.focus();
    }
  }, [uploadOpen, isOpen, view]);

  useEffect(
    () => () => {
      turnGenerationRef.current += 1;
      abortRef.current?.abort();
    },
    []
  );

  const runTurn = useCallback(
    async (history: ChatEntry[]) => {
      if (getHankSessionScope() !== sessionScopeRef.current) return;
      const generation = ++turnGenerationRef.current;
      setBusy(true);
      setActivity(null);
      setStreamText('');
      const controller = new AbortController();
      abortRef.current = controller;
      const isCurrentTurn = () =>
        generation === turnGenerationRef.current &&
        !controller.signal.aborted &&
        getHankSessionScope() === sessionScopeRef.current;
      try {
        const final = await api.copilotChatStream(
          { messages: toApiMessages(history), context_hint: contextHint },
          {
            onToolUse: (_tool, summary) => {
              if (isCurrentTurn()) setActivity(summary);
            },
            onDelta: text => {
              if (!isCurrentTurn()) return;
              setActivity(null);
              setStreamText(prev => prev + text);
            },
          },
          controller.signal
        );
        if (!isCurrentTurn()) return;
        replaceEntries([
          ...entriesRef.current,
          {
            role: 'assistant',
            content: final.answer,
            references: final.references,
            toolTrace: final.tool_trace,
            truncated: final.truncated,
          },
        ]);
      } catch (err: unknown) {
        if (isCurrentTurn() && !(err instanceof DOMException && err.name === 'AbortError')) {
          replaceEntries([
            ...entriesRef.current,
            {
              role: 'assistant',
              content: err instanceof Error ? err.message : 'Hank is unavailable right now.',
              error: true,
            },
          ]);
        }
      } finally {
        if (generation === turnGenerationRef.current) {
          setBusy(false);
          setActivity(null);
          setStreamText('');
          abortRef.current = null;
        }
      }
    },
    [contextHint, replaceEntries]
  );

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || abortRef.current || uploadOpen || getHankSessionScope() !== sessionScopeRef.current) return;
      setInput('');
      const next: ChatEntry[] = [...entriesRef.current, { role: 'user', content: trimmed }];
      replaceEntries(next);
      void runTurn(next);
    },
    [uploadOpen, runTurn, replaceEntries]
  );

  const retry = useCallback(() => {
    if (abortRef.current || uploadOpen) return;
    const next = [...entriesRef.current];
    if (next[next.length - 1]?.error) next.pop();
    if (!next.length || next[next.length - 1].role !== 'user') return;
    replaceEntries(next);
    void runTurn(next);
  }, [uploadOpen, runTurn, replaceEntries]);

  const stop = useCallback(() => {
    turnGenerationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    setStreamText('');
    setActivity(null);
  }, []);

  const clear = useCallback(() => {
    if (uploadBusy) return;
    stop();
    replaceEntries([]);
    setInput('');
  }, [stop, replaceEntries, uploadBusy]);

  const handleUploaded = (document: { id: number; document_number: string; title: string; revision: string }) => {
    if (getHankSessionScope() !== sessionScopeRef.current) return;
    replaceEntries([
      ...entriesRef.current,
      { role: 'user', content: `File PDF: ${document.title}` },
      {
        role: 'assistant',
        content: `Filed ${document.title} as ${document.document_number}, revision ${document.revision}. The PDF is saved and released in Documents.`,
        references: [
          {
            type: 'document',
            id: document.id,
            label: `${document.document_number} · Rev ${document.revision}`,
            url: `/documents?document=${document.id}`,
          },
        ],
      },
    ]);
    setUploadOpen(false);
    setUploadBusy(false);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send(input);
    }
  };

  const lastEntryFailed = entries.length > 0 && entries[entries.length - 1].error;

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/50 backdrop-blur-[2px]"
          onClick={onClose}
          aria-hidden="true"
          data-testid="copilot-backdrop"
        />
      )}
      <aside
        ref={panelRef}
        inert={!isOpen}
        hidden={!isOpen}
        aria-modal={isOpen ? true : undefined}
        role="dialog"
        aria-label="Hank"
        aria-hidden={!isOpen}
        className={`fixed inset-y-0 right-0 z-50 w-full max-w-md flex flex-col transform transition-transform duration-200 ease-out ${
          isOpen ? 'translate-x-0' : 'translate-x-full pointer-events-none'
        }`}
        style={{
          display: isOpen ? 'flex' : 'none',
          background: 'var(--fd-panel)',
          borderLeft: '1px solid var(--fd-line)',
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between h-14 px-4 flex-shrink-0"
          style={{ borderBottom: '1px solid var(--fd-line)' }}
        >
          <div className="flex items-center gap-2 min-w-0">
            <HankAvatar className="h-9 w-9 flex-shrink-0" />
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-fd-ink leading-4">Hank</h2>
              <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-fd-mute truncate">
                AI shop teammate
              </p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            {entries.length > 0 && (
              <button
                type="button"
                onClick={clear}
                disabled={uploadBusy}
                className="p-2 rounded-[3px] text-fd-mute hover:text-fd-ink hover:bg-white/5 transition-colors"
                title="Clear conversation"
                aria-label="Clear conversation"
              >
                <TrashIcon className="h-4 w-4" />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="p-2 rounded-[3px] text-fd-mute hover:text-fd-ink hover:bg-white/5 transition-colors"
              aria-label="Close Hank"
            >
              <XMarkIcon className="h-5 w-5" />
            </button>
          </div>
        </div>

        <div className="px-4 py-3 flex-shrink-0" style={{ borderBottom: '1px solid var(--fd-line)' }}>
          <p className="text-xs text-fd-mute">
            Check your shift, manage tasks, and prepare work.
            {canUpload ? ' File PDFs here, too.' : ' Ask Hank for the records behind each answer.'}
          </p>
          {canUpload && (
            <button
              type="button"
              onClick={() => {
                setView('chat');
                setUploadOpen(true);
              }}
              disabled={busy || uploadOpen || taskBusy || workBusy || preferencesBusy}
              className="mt-2 inline-flex items-center gap-2 px-3 py-1.5 rounded-[3px] text-xs text-fd-ink hover:bg-white/5 disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ border: '1px solid var(--fd-line-bright)' }}
            >
              <ArrowUpTrayIcon className="h-4 w-4 text-fd-amber" />
              Upload PDF
            </button>
          )}
        </div>

        {!uploadOpen && (
          <nav aria-label="Hank workspace" className="flex flex-wrap gap-1 px-4 py-2 border-b border-slate-700">
            {(
              [
                { id: 'briefing', label: 'My shift' },
                { id: 'chat', label: 'Chat' },
                { id: 'tasks', label: 'Tasks' },
                { id: 'work', label: 'Work' },
                { id: 'preferences', label: 'Preferences' },
              ] as const
            ).map(tab => (
              <button
                key={tab.id}
                type="button"
                aria-pressed={view === tab.id}
                disabled={busy || taskBusy || workBusy || preferencesBusy}
                onClick={() => setView(tab.id)}
                className={`px-3 py-1.5 text-xs rounded-[3px] ${view === tab.id ? 'bg-slate-700 text-fd-ink' : 'text-fd-mute'}`}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        )}

        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {!uploadOpen && view === 'briefing' && <HankBriefing onNavigate={onClose} />}
          {!uploadOpen && view === 'tasks' && (
            <HankTaskWorkspace key={taskNavigation} taskId={taskId} onNavigate={onClose} onBusyChange={setTaskBusy} />
          )}
          {!uploadOpen && view === 'work' && (
            <HankWorkWorkspace
              key={workNavigation}
              context={recordContext}
              initialArea={workSelection.area}
              initialId={workSelection.id}
              onNavigate={onClose}
              onBusyChange={setWorkBusy}
            />
          )}
          {!uploadOpen && view === 'preferences' && <HankPreferences onBusyChange={setPreferencesBusy} />}
          {uploadOpen && canUpload && (
            <HankDocumentUpload
              workOrderId={workOrderId}
              onUploaded={handleUploaded}
              onBusyChange={setUploadBusy}
              onCancel={() => {
                if (!uploadBusy) setUploadOpen(false);
              }}
            />
          )}
          {entries.length === 0 && !busy && !uploadOpen && view === 'chat' && (
            <div className="space-y-3">
              <p className="text-sm text-fd-body">
                I’m Hank, named after the shop’s yellow Lab. I can help you find a job, check blockers, or look up
                stock.
                {canUpload && ' Have a PDF to file? Use Upload PDF and review its details before saving.'}
              </p>
              <div className="space-y-1.5">
                {suggestions.map(suggestion => (
                  <button
                    key={suggestion.label}
                    type="button"
                    onClick={() => {
                      if (suggestion.draft) {
                        setInput(suggestion.prompt);
                        inputRef.current?.focus();
                      } else {
                        send(suggestion.prompt);
                      }
                    }}
                    className="block w-full text-left px-3 py-2 rounded-[3px] text-[13px] text-fd-body hover:text-fd-ink hover:bg-white/[0.03] transition-colors"
                    style={{ border: '1px solid var(--fd-line)' }}
                  >
                    {suggestion.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {!uploadOpen &&
            view === 'chat' &&
            entries.slice(-MAX_DISPLAYED_ENTRIES).map((entry, index) => (
              <div key={index} data-testid={`copilot-message-${entry.role}`}>
                {entry.role === 'user' ? (
                  <div className="flex justify-end">
                    <div
                      className="max-w-[85%] px-3 py-2 rounded-[3px] text-[13px] text-fd-ink whitespace-pre-wrap"
                      style={{ background: 'rgba(47,129,247,0.12)', border: '1px solid rgba(47,129,247,0.35)' }}
                    >
                      {entry.content}
                    </div>
                  </div>
                ) : (
                  <div
                    className="max-w-[95%] px-3 py-2 rounded-[3px]"
                    style={{
                      background: entry.error ? 'rgba(200,53,43,0.08)' : 'var(--fd-raised)',
                      border: `1px solid ${entry.error ? 'rgba(200,53,43,0.45)' : 'var(--fd-line)'}`,
                    }}
                  >
                    <p className={`text-[13px] whitespace-pre-wrap ${entry.error ? 'text-red-300' : 'text-fd-body'}`}>
                      {entry.content}
                    </p>
                    {entry.truncated && (
                      <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.06em] text-fd-amber">
                        lookup limit reached — partial answer
                      </p>
                    )}
                    {entry.error && entry === entries[entries.length - 1] && (
                      <button
                        type="button"
                        onClick={retry}
                        disabled={busy || uploadOpen}
                        className="mt-2 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[3px] font-mono text-[11px] text-fd-ink hover:bg-white/5 transition-colors"
                        style={{ border: '1px solid var(--fd-line)' }}
                      >
                        <ArrowPathIcon className="h-3.5 w-3.5" />
                        Retry
                      </button>
                    )}
                    {!!entry.references?.length && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {entry.references.map(reference => (
                          <Link
                            key={`${reference.type}-${reference.id}`}
                            to={reference.url}
                            onClick={() => {
                              if (reference.type === 'hank_task') setView('tasks');
                              else onClose();
                            }}
                            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[3px] font-mono text-[11px] text-fd-blue hover:text-fd-ink hover:bg-white/5 transition-colors"
                            style={{ border: '1px solid var(--fd-line-bright)' }}
                          >
                            {reference.label}
                          </Link>
                        ))}
                      </div>
                    )}
                    {!!entry.toolTrace?.length && (
                      <p className="mt-1.5 font-mono text-[10px] text-fd-faint truncate">
                        {entry.toolTrace.map(trace => trace.summary).join(' · ')}
                      </p>
                    )}
                  </div>
                )}
              </div>
            ))}

          {busy && !uploadOpen && (
            <div
              className="max-w-[95%] px-3 py-2 rounded-[3px]"
              style={{ background: 'var(--fd-raised)', border: '1px solid var(--fd-line)' }}
            >
              {streamText ? (
                <p className="text-[13px] text-fd-body whitespace-pre-wrap" data-testid="copilot-streaming">
                  {streamText}
                  <span className="inline-block w-1.5 h-3.5 ml-0.5 align-middle bg-fd-blue animate-pulse" />
                </p>
              ) : (
                <p className="font-mono text-[11px] text-fd-mute animate-pulse" data-testid="copilot-activity">
                  {activity || 'Hank is looking into it…'}
                </p>
              )}
            </div>
          )}
        </div>

        {/* Composer */}
        {!uploadOpen && view === 'chat' && (
          <div className="flex-shrink-0 p-3" style={{ borderTop: '1px solid var(--fd-line)' }}>
            <div
              className="flex items-end gap-2 px-3 py-2 rounded-[3px]"
              style={{ background: 'var(--fd-sunken)', border: '1px solid var(--fd-line)' }}
            >
              <textarea
                ref={inputRef}
                value={input}
                onChange={event => setInput(event.target.value)}
                onKeyDown={handleKeyDown}
                rows={1}
                maxLength={8000}
                disabled={uploadOpen}
                placeholder="Ask Hank about a job, blocker, or part…"
                aria-label="Ask Hank"
                className="flex-1 resize-none bg-transparent text-[13px] text-fd-ink placeholder:text-fd-faint focus:outline-none max-h-32"
              />
              {busy ? (
                <button
                  type="button"
                  onClick={stop}
                  className="p-1.5 rounded-[3px] text-fd-mute hover:bg-white/5"
                  aria-label="Stop Hank's answer"
                >
                  <StopIcon className="h-4 w-4" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => send(input)}
                  disabled={uploadOpen || !input.trim()}
                  className="p-1.5 rounded-[3px] text-fd-blue hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  aria-label="Send message"
                >
                  <PaperAirplaneIcon className="h-4 w-4" />
                </button>
              )}
            </div>
            <div className="mt-2">
              <HankVoiceInput
                disabled={busy || !isOpen}
                onTranscript={text => setInput(previous => [previous, text].filter(Boolean).join(' ').slice(0, 8000))}
              />
            </div>
            {lastEntryFailed ? (
              <p className="mt-1.5 font-mono text-[10px] text-red-300">Last request failed — retry or rephrase.</p>
            ) : (
              <p className="mt-1.5 text-[10px] text-fd-mute">
                Tasks require your review and submission. {canUpload && 'PDF filing releases on submission. '}
                Conversation clears on reload.
              </p>
            )}
          </div>
        )}
      </aside>
    </>
  );
}

export default CopilotPanel;
