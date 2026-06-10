/**
 * Werco Copilot — read-only ask-anything chat drawer over the tenant's ERP data.
 *
 * - Opens from the header sparkles button or Ctrl+. (wired in Layout).
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
  PaperAirplaneIcon,
  SparklesIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { CopilotMessage, CopilotReference, CopilotToolTraceEntry } from '../../types/copilot';

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
const SUGGESTIONS = ["What's blocked right now?", 'How loaded is the laser this week?', 'Anything overdue?'];

function toApiMessages(entries: ChatEntry[]): CopilotMessage[] {
  return entries
    .filter((entry) => !entry.error && entry.content.trim().length > 0)
    .map((entry) => ({ role: entry.role, content: entry.content }))
    .slice(-MAX_HISTORY_SENT);
}

export function CopilotPanel({ isOpen, onClose }: CopilotPanelProps) {
  const location = useLocation();
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [activity, setActivity] = useState<string | null>(null);
  const [streamText, setStreamText] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Mirror of `entries` so send/retry can compute the next history without
  // putting side effects inside a state updater (StrictMode double-invokes those).
  const entriesRef = useRef<ChatEntry[]>(entries);
  useEffect(() => {
    entriesRef.current = entries;
  }, [entries]);

  const contextHint = useMemo(() => `viewing ${location.pathname}${location.search}`, [location]);

  useEffect(() => {
    if (isOpen) {
      const id = window.setTimeout(() => inputRef.current?.focus(), 150);
      return () => window.clearTimeout(id);
    }
    return undefined;
  }, [isOpen]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && typeof el.scrollTo === 'function') {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [entries, streamText, activity, isOpen]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const runTurn = useCallback(
    async (history: ChatEntry[]) => {
      setBusy(true);
      setActivity(null);
      setStreamText('');
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const final = await api.copilotChatStream(
          { messages: toApiMessages(history), context_hint: contextHint },
          {
            onToolUse: (_tool, summary) => setActivity(summary),
            onDelta: (text) => {
              setActivity(null);
              setStreamText((prev) => prev + text);
            },
          },
          controller.signal
        );
        setEntries((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: final.answer,
            references: final.references,
            toolTrace: final.tool_trace,
            truncated: final.truncated,
          },
        ]);
      } catch (err: unknown) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          setEntries((prev) => [
            ...prev,
            {
              role: 'assistant',
              content: err instanceof Error ? err.message : 'The copilot is unavailable right now.',
              error: true,
            },
          ]);
        }
      } finally {
        setBusy(false);
        setActivity(null);
        setStreamText('');
        abortRef.current = null;
      }
    },
    [contextHint]
  );

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || busy) return;
      setInput('');
      const next: ChatEntry[] = [...entriesRef.current, { role: 'user', content: trimmed }];
      setEntries(next);
      void runTurn(next);
    },
    [busy, runTurn]
  );

  const retry = useCallback(() => {
    if (busy) return;
    const next = [...entriesRef.current];
    while (next.length && (next[next.length - 1].error || next[next.length - 1].role === 'assistant')) {
      next.pop();
    }
    if (!next.length || next[next.length - 1].role !== 'user') return;
    setEntries(next);
    void runTurn(next);
  }, [busy, runTurn]);

  const clear = useCallback(() => {
    abortRef.current?.abort();
    setEntries([]);
    setStreamText('');
    setActivity(null);
  }, []);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send(input);
    }
    if (event.key === 'Escape') {
      onClose();
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
        role="dialog"
        aria-label="Werco Copilot"
        aria-hidden={!isOpen}
        className={`fixed inset-y-0 right-0 z-50 w-full max-w-md flex flex-col transform transition-transform duration-200 ease-out ${
          isOpen ? 'translate-x-0' : 'translate-x-full pointer-events-none'
        }`}
        style={{ background: 'var(--fd-panel)', borderLeft: '1px solid var(--fd-line)' }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between h-14 px-4 flex-shrink-0"
          style={{ borderBottom: '1px solid var(--fd-line)' }}
        >
          <div className="flex items-center gap-2 min-w-0">
            <SparklesIcon className="h-5 w-5 text-fd-blue flex-shrink-0" />
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-fd-ink leading-4">Werco Copilot</h2>
              <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-fd-mute truncate">
                read-only · your company data
              </p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            {entries.length > 0 && (
              <button
                type="button"
                onClick={clear}
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
              aria-label="Close copilot"
            >
              <XMarkIcon className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {entries.length === 0 && !busy && (
            <div className="space-y-3">
              <p className="text-sm text-fd-body">
                Ask about jobs, blockers, schedule load, inventory, or customers. The copilot only reads data — it
                never changes anything.
              </p>
              <div className="space-y-1.5">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => send(suggestion)}
                    className="block w-full text-left px-3 py-2 rounded-[3px] text-[13px] text-fd-body hover:text-fd-ink hover:bg-white/[0.03] transition-colors"
                    style={{ border: '1px solid var(--fd-line)' }}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {entries.map((entry, index) => (
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
                  {entry.error && (
                    <button
                      type="button"
                      onClick={retry}
                      className="mt-2 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[3px] font-mono text-[11px] text-fd-ink hover:bg-white/5 transition-colors"
                      style={{ border: '1px solid var(--fd-line)' }}
                    >
                      <ArrowPathIcon className="h-3.5 w-3.5" />
                      Retry
                    </button>
                  )}
                  {!!entry.references?.length && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {entry.references.map((reference) => (
                        <Link
                          key={`${reference.type}-${reference.id}`}
                          to={reference.url}
                          onClick={onClose}
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
                      {entry.toolTrace.map((trace) => trace.summary).join(' · ')}
                    </p>
                  )}
                </div>
              )}
            </div>
          ))}

          {busy && (
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
                  {activity || 'thinking…'}
                </p>
              )}
            </div>
          )}
        </div>

        {/* Composer */}
        <div className="flex-shrink-0 p-3" style={{ borderTop: '1px solid var(--fd-line)' }}>
          <div
            className="flex items-end gap-2 px-3 py-2 rounded-[3px]"
            style={{ background: 'var(--fd-sunken)', border: '1px solid var(--fd-line)' }}
          >
            <textarea
              ref={inputRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleKeyDown}
              rows={1}
              maxLength={8000}
              placeholder="Ask about a job, blocker, or part…"
              aria-label="Ask the copilot"
              className="flex-1 resize-none bg-transparent text-[13px] text-fd-ink placeholder:text-fd-faint focus:outline-none max-h-32"
            />
            <button
              type="button"
              onClick={() => send(input)}
              disabled={busy || !input.trim()}
              className="p-1.5 rounded-[3px] text-fd-blue hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              aria-label="Send message"
            >
              <PaperAirplaneIcon className="h-4 w-4" />
            </button>
          </div>
          {lastEntryFailed ? (
            <p className="mt-1.5 font-mono text-[10px] text-red-300">Last request failed — retry or rephrase.</p>
          ) : (
            <p className="mt-1.5 font-mono text-[10px] text-fd-faint">Enter to send · Shift+Enter for a new line</p>
          )}
        </div>
      </aside>
    </>
  );
}

export default CopilotPanel;
