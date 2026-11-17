import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ChatBubbleLeftRightIcon, EyeIcon as EyeOutlineIcon } from '@heroicons/react/24/outline';
import { EyeIcon as EyeSolidIcon, PencilSquareIcon, TrashIcon } from '@heroicons/react/20/solid';
import api from '../../services/api';
import { CockpitPanel } from '../cockpit';
import { Button } from '../ui/Button';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { EmptyState } from '../ui/EmptyState';
import { ErrorState } from '../ui/ErrorState';
import { LoadingButton } from '../ui/LoadingButton';
import { SkeletonText } from '../ui/Skeleton';
import { useToast } from '../ui/Toast';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { apiErrorDetail } from '../../utils/apiError';
import { formatCentralDateTime } from '../../utils/centralTime';
import {
  COMMENT_BODY_MAX_LENGTH,
  type CommentEntityType,
  type CommentPerson,
  type CommentResponse,
  type WatchState,
} from '../../types/comments';
import MentionTextarea from './MentionTextarea';

/**
 * The comment thread on a record (v1: a work order), with @mentions and a
 * Watch toggle.
 *
 * Writes are NON-optimistic: a posted, edited or deleted comment changes the
 * thread only once the server has answered, and a refusal (e.g. a 422 naming a
 * person who can't be mentioned) leaves the draft exactly as typed.
 *
 * The thread refreshes on its own 30 s interval — realtime is dark, and the
 * page's poll re-runs only the work-order load — skipped while the tab is
 * hidden and run again when it becomes visible. A refresh replaces only the
 * THREAD: the composer draft and an open edit are separate state, so a poll
 * never clobbers what someone is typing. Responses are sequenced, so a poll
 * that was in flight when a write landed cannot roll the thread back.
 */

export const COMMENTS_POLL_INTERVAL_MS = 30_000;

export interface CommentsPanelProps {
  entityType: CommentEntityType;
  entityId: number;
  className?: string;
}

interface EditState {
  id: number;
  body: string;
  mentions: CommentPerson[];
  originalBody: string;
  originalMentionIds: number[];
}

/** Same people, in any order. */
function sameIds(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false;
  const sortedB = [...b].sort((x, y) => x - y);
  return [...a].sort((x, y) => x - y).every((id, i) => id === sortedB[i]);
}

/** Insert or replace by id, keeping the thread oldest-first. */
function upsertComment(list: CommentResponse[], comment: CommentResponse): CommentResponse[] {
  const index = list.findIndex(item => item.id === comment.id);
  if (index >= 0) return list.map(item => (item.id === comment.id ? comment : item));
  return [...list, comment];
}

const RECORD_NOUN: Record<CommentEntityType, string> = { work_order: 'work order' };

export function CommentsPanel({ entityType, entityId, className }: CommentsPanelProps) {
  const { showToast } = useToast();
  const recordNoun = RECORD_NOUN[entityType];

  const [comments, setComments] = useState<CommentResponse[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [watch, setWatch] = useState<WatchState | null>(null);
  const [watchPending, setWatchPending] = useState(false);

  const [draft, setDraft] = useState('');
  const [draftMentions, setDraftMentions] = useState<CommentPerson[]>([]);
  const [posting, setPosting] = useState(false);
  const [postError, setPostError] = useState<string | null>(null);

  const [editing, setEditing] = useState<EditState | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<CommentResponse | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Every load and every write takes a ticket; a load applies its result only
  // if no newer load or write happened while it was in flight.
  const seqRef = useRef(0);
  const hasThreadRef = useRef(false);
  const threadRef = useRef<HTMLOListElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  // Each comment's Edit button, so focus can return to it when its editor
  // closes (saved or cancelled) instead of dropping to <body>.
  const editButtonRefs = useRef(new Map<number, HTMLButtonElement>());
  const focusEditButtonRef = useRef<number | null>(null);
  // Scroll the thread to its newest comment after the first load and after a
  // post of our own — never on a background poll, which would yank whatever
  // someone is reading.
  const scrollToEndRef = useRef(true);

  const composerDirty = draft.trim() !== '' || draftMentions.length > 0;
  const editDirty =
    editing !== null &&
    (editing.body.trim() !== editing.originalBody.trim() ||
      !sameIds(
        editing.mentions.map(person => person.id),
        editing.originalMentionIds
      ));
  const { markSaved, confirmDiscard } = useUnsavedChanges(
    composerDirty || editDirty,
    'You have an unsent comment. Discard it?'
  );

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const seq = ++seqRef.current;
      if (mode === 'initial') {
        setLoading(true);
        setLoadError(null);
      }
      const [listResult, watchResult] = await Promise.allSettled([
        api.listComments(entityType, entityId),
        api.getWatchState(entityType, entityId),
      ]);
      if (seq !== seqRef.current) return;
      if (listResult.status === 'fulfilled') {
        hasThreadRef.current = true;
        setComments(listResult.value);
        setLoadError(null);
        setRefreshFailed(false);
      } else if (mode === 'initial' || !hasThreadRef.current) {
        setLoadError(apiErrorDetail(listResult.reason, 'Comments could not be loaded.'));
      } else {
        // Keep the thread already on screen; say it may be stale.
        setRefreshFailed(true);
      }
      // A watch-state failure hides nothing but the toggle's current value.
      if (watchResult.status === 'fulfilled') setWatch(watchResult.value);
      setLoading(false);
    },
    [entityType, entityId]
  );

  useEffect(() => {
    hasThreadRef.current = false;
    scrollToEndRef.current = true;
    setComments(null);
    setWatch(null);
    void load('initial');
  }, [load]);

  useEffect(() => {
    const refreshIfVisible = () => {
      if (document.visibilityState === 'visible') void load('refresh');
    };
    const interval = window.setInterval(refreshIfVisible, COMMENTS_POLL_INTERVAL_MS);
    document.addEventListener('visibilitychange', refreshIfVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshIfVisible);
    };
  }, [load]);

  useLayoutEffect(() => {
    if (!comments || !scrollToEndRef.current) return;
    scrollToEndRef.current = false;
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [comments]);

  // The comment being edited was deleted (by an Admin/Manager, seen on a poll):
  // there is nothing left to save to, so close the editor and say why.
  useEffect(() => {
    if (!editing || !comments) return;
    if (!comments.some(comment => comment.id === editing.id)) {
      setEditing(null);
      setEditError(null);
      showToast('warning', 'The comment you were editing has been deleted.');
    }
  }, [comments, editing, showToast]);

  useEffect(() => {
    if (editing !== null) return;
    const id = focusEditButtonRef.current;
    if (id === null) return;
    focusEditButtonRef.current = null;
    editButtonRefs.current.get(id)?.focus();
  }, [editing]);

  /** The server auto-watches commenters and mentioned people — re-read after a write. */
  const refreshWatch = useCallback(async () => {
    try {
      setWatch(await api.getWatchState(entityType, entityId));
    } catch {
      // Not worth a toast: the toggle keeps its last known value and the next
      // poll re-reads it.
    }
  }, [entityType, entityId]);

  const draftBody = draft.trim();
  const draftPostable = draftBody.length > 0 && draftBody.length <= COMMENT_BODY_MAX_LENGTH;

  const handlePost = async () => {
    if (posting || !draftPostable) return;
    setPosting(true);
    setPostError(null);
    try {
      const created = await api.createComment({
        entity_type: entityType,
        entity_id: entityId,
        body: draftBody,
        mentioned_user_ids: draftMentions.map(person => person.id),
      });
      seqRef.current += 1;
      hasThreadRef.current = true;
      scrollToEndRef.current = true;
      setComments(previous => upsertComment(previous ?? [], created));
      setDraft('');
      setDraftMentions([]);
      if (!editDirty) markSaved();
      showToast('success', 'Comment posted');
      // Ready for the next comment (the Post button just went disabled).
      composerRef.current?.focus();
      void refreshWatch();
    } catch (err) {
      const detail = apiErrorDetail(err, 'The comment could not be posted.');
      setPostError(detail);
      showToast('error', detail);
    } finally {
      setPosting(false);
    }
  };

  const startEdit = (comment: CommentResponse) => {
    if (editing && editing.id !== comment.id && editDirty && !confirmDiscard()) return;
    setEditError(null);
    setEditing({
      id: comment.id,
      body: comment.body,
      mentions: comment.mentions,
      originalBody: comment.body,
      originalMentionIds: comment.mentions.map(person => person.id),
    });
  };

  const cancelEdit = () => {
    if (editDirty && !confirmDiscard()) return;
    focusEditButtonRef.current = editing?.id ?? null;
    setEditing(null);
    setEditError(null);
  };

  const editBody = editing ? editing.body.trim() : '';
  const editSavable = editDirty && editBody.length > 0 && editBody.length <= COMMENT_BODY_MAX_LENGTH;

  const handleSaveEdit = async () => {
    if (!editing || savingEdit || !editSavable) return;
    setSavingEdit(true);
    setEditError(null);
    try {
      // PATCH carries the FULL new state: both fields, every time.
      const updated = await api.updateComment(editing.id, {
        body: editBody,
        mentioned_user_ids: editing.mentions.map(person => person.id),
      });
      seqRef.current += 1;
      setComments(previous => upsertComment(previous ?? [], updated));
      focusEditButtonRef.current = updated.id;
      setEditing(null);
      if (!composerDirty) markSaved();
      showToast('success', 'Comment updated');
      void refreshWatch();
    } catch (err) {
      const detail = apiErrorDetail(err, 'The comment could not be saved.');
      setEditError(detail);
      showToast('error', detail);
    } finally {
      setSavingEdit(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setDeleting(true);
    try {
      await api.deleteComment(target.id);
      seqRef.current += 1;
      setComments(previous => (previous ?? []).filter(comment => comment.id !== target.id));
      if (editing?.id === target.id) {
        setEditing(null);
        setEditError(null);
      }
      setDeleteTarget(null);
      showToast('success', 'Comment deleted');
    } catch (err) {
      showToast('error', apiErrorDetail(err, 'The comment could not be deleted.'));
      setDeleteTarget(null);
      // The refusal may be because it is already gone, or no longer ours to delete.
      void load('refresh');
    } finally {
      setDeleting(false);
    }
  };

  const toggleWatch = async () => {
    if (!watch || watchPending) return;
    setWatchPending(true);
    try {
      const next = watch.watching
        ? await api.unwatchRecord(entityType, entityId)
        : await api.watchRecord(entityType, entityId);
      seqRef.current += 1;
      setWatch(next);
    } catch (err) {
      showToast('error', apiErrorDetail(err, 'Your watch setting could not be changed.'));
    } finally {
      setWatchPending(false);
    }
  };

  const watching = watch?.watching ?? false;
  const watchControl = (
    <div className="flex items-center gap-2">
      {watch && (
        <span className="text-xs tabular-nums text-slate-400">
          {watch.watcher_count} watching
        </span>
      )}
      {/* A toggle: the accessible name stays "Watch" and aria-pressed carries
          the state (APG), so the label never flips under a screen reader. */}
      <LoadingButton
        variant={watching ? 'primary' : 'secondary'}
        size="sm"
        aria-pressed={watching}
        loading={watchPending}
        focusableWhileLoading
        disabled={!watch}
        title={
          watching
            ? `You are watching this ${recordNoun}: new comments notify you. Press to stop watching.`
            : `Watch this ${recordNoun} to be notified of new comments.`
        }
        onClick={toggleWatch}
        className="gap-1.5"
      >
        {watching ? (
          <EyeSolidIcon className="h-4 w-4" aria-hidden="true" />
        ) : (
          <EyeOutlineIcon className="h-4 w-4" aria-hidden="true" />
        )}
        Watch
      </LoadingButton>
    </div>
  );

  let thread: React.ReactNode;
  if (loading && comments === null) {
    thread = (
      <div role="status" aria-busy="true" aria-label="Loading comments">
        <SkeletonText lines={3} />
      </div>
    );
  } else if (loadError && comments === null) {
    thread = (
      <ErrorState title="Couldn't load comments" message={loadError} onRetry={() => void load('initial')} />
    );
  } else if (!comments || comments.length === 0) {
    thread = (
      <EmptyState
        icon={ChatBubbleLeftRightIcon}
        title="No comments yet"
        description={`Start the conversation about this ${recordNoun}. Type @ to mention someone and notify them.`}
        className="!py-6"
      />
    );
  } else {
    thread = (
      <ol ref={threadRef} aria-label="Comments" className="max-h-[28rem] space-y-2 overflow-y-auto pr-1">
        {comments.map(comment => {
          const postedAt = formatCentralDateTime(comment.created_at);
          const isEditing = editing?.id === comment.id;
          return (
            <li key={comment.id} className="border border-fd-line bg-fd-sunken/40 px-3 py-2">
              <div className="flex items-start justify-between gap-2">
                <p className="min-w-0 text-xs text-slate-400">
                  <span className="font-semibold text-fd-ink">{comment.author.display_name}</span>
                  <span aria-hidden="true"> · </span>
                  <time dateTime={comment.created_at}>{postedAt}</time>
                  {comment.edited_at && (
                    <span className="ml-1 text-slate-500" title={`Edited ${formatCentralDateTime(comment.edited_at)}`}>
                      (edited<span className="sr-only"> {formatCentralDateTime(comment.edited_at)}</span>)
                    </span>
                  )}
                </p>
                {!isEditing && (comment.can_edit || comment.can_delete) && (
                  <div className="flex flex-shrink-0 items-center gap-1">
                    {comment.can_edit && (
                      <Button
                        ref={el => {
                          if (el) editButtonRefs.current.set(comment.id, el);
                          else editButtonRefs.current.delete(comment.id);
                        }}
                        variant="ghost"
                        size="sm"
                        aria-label={`Edit comment from ${postedAt}`}
                        title="Edit"
                        onClick={() => startEdit(comment)}
                        className="!px-1.5"
                      >
                        <PencilSquareIcon className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    )}
                    {comment.can_delete && (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Delete comment by ${comment.author.display_name} from ${postedAt}`}
                        title="Delete"
                        onClick={() => setDeleteTarget(comment)}
                        className="!px-1.5 hover:text-fd-red"
                      >
                        <TrashIcon className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    )}
                  </div>
                )}
              </div>

              {isEditing && editing ? (
                <div className="mt-2 space-y-2">
                  <MentionTextarea
                    label="Edit comment"
                    value={editing.body}
                    onChange={body => {
                      setEditing(current => (current ? { ...current, body } : current));
                      if (editError) setEditError(null);
                    }}
                    mentions={editing.mentions}
                    onMentionsChange={mentions => {
                      setEditing(current => (current ? { ...current, mentions } : current));
                      if (editError) setEditError(null);
                    }}
                    readOnly={savingEdit}
                    error={editError}
                    onSubmitShortcut={handleSaveEdit}
                  />
                  <div className="flex items-center gap-2">
                    <LoadingButton
                      size="sm"
                      loading={savingEdit}
                      loadingText="Saving…"
                      focusableWhileLoading
                      disabled={!editSavable}
                      onClick={handleSaveEdit}
                    >
                      Save comment
                    </LoadingButton>
                    <Button variant="secondary" size="sm" onClick={cancelEdit} disabled={savingEdit}>
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  {/* Plain text, whitespace kept: never rendered as HTML. */}
                  <p className="mt-1 whitespace-pre-wrap break-words text-sm text-fd-body">{comment.body}</p>
                  {comment.mentions.length > 0 && (
                    <ul aria-label="Mentioned" className="mt-1.5 flex flex-wrap gap-1">
                      {comment.mentions.map(person => (
                        <li
                          key={person.id}
                          className="border border-fd-blue/30 bg-fd-blue/10 px-1.5 py-0.5 text-[11px] text-fd-ink"
                        >
                          @{person.display_name}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </li>
          );
        })}
      </ol>
    );
  }

  return (
    <CockpitPanel
      title="Comments"
      subtitle={`Discuss this ${recordNoun}. @mention people to notify them.`}
      className={className}
      bodyClassName="space-y-3 lg:max-h-none"
      headerExtra={watchControl}
      footer={comments && comments.length > 0 ? String(comments.length) : undefined}
    >
      {refreshFailed && comments && (
        <div role="status" className="flex items-center justify-between gap-2 border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-200">
          <span>Comments could not be refreshed; showing the last loaded thread.</span>
          <Button variant="ghost" size="sm" onClick={() => void load('refresh')}>
            Retry
          </Button>
        </div>
      )}

      {thread}

      <div className="space-y-2 border-t border-fd-line pt-3">
        <MentionTextarea
          ref={composerRef}
          label="Add a comment"
          value={draft}
          onChange={value => {
            setDraft(value);
            if (postError) setPostError(null);
          }}
          mentions={draftMentions}
          onMentionsChange={mentions => {
            setDraftMentions(mentions);
            if (postError) setPostError(null);
          }}
          readOnly={posting}
          placeholder="Write a comment…"
          error={postError}
          onSubmitShortcut={handlePost}
        />
        <div className="flex items-center justify-between gap-2">
          <p className="text-[11px] text-slate-500">Ctrl+Enter to post. Mentioned people are notified (without the comment text).</p>
          <LoadingButton
            size="sm"
            loading={posting}
            loadingText="Posting…"
            focusableWhileLoading
            disabled={!draftPostable}
            onClick={handlePost}
          >
            Post comment
          </LoadingButton>
        </div>
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete comment?"
        message={
          deleteTarget
            ? `Delete ${deleteTarget.can_edit ? 'your' : `${deleteTarget.author.display_name}'s`} comment from ${formatCentralDateTime(deleteTarget.created_at)}? It will no longer appear on this ${recordNoun}.`
            : ''
        }
        confirmLabel="Delete comment"
        pending={deleting}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </CockpitPanel>
  );
}

export default CommentsPanel;
