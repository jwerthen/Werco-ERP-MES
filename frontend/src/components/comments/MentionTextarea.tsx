import React, {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { XMarkIcon } from '@heroicons/react/20/solid';
import api from '../../services/api';
import { FormField } from '../ui/FormField';
import { useModalPortal } from '../ui/Modal';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import {
  COMMENT_BODY_MAX_LENGTH,
  MAX_MENTIONS_PER_COMMENT,
  type CommentPerson,
} from '../../types/comments';

/**
 * A comment textarea with structured @mentions.
 *
 * ---------------------------------------------------------------------------
 * THE CHIPS ARE THE MENTION, NOT THE TEXT
 * ---------------------------------------------------------------------------
 * Typing `@` followed by letters opens a list of people from
 * `GET /users/mention-search`. Picking one does two things: it writes
 * `@Display Name` into the text, and it adds that person's id to `mentions`,
 * shown as removable chips under the field. The server never parses names out
 * of the comment body — it receives `mentioned_user_ids` and notifies exactly
 * those people — so the chips are the only truth about who gets notified.
 *
 * The two are deliberately NOT kept in sync after the pick:
 *   - deleting the `@Name` text does NOT remove the chip (the person is still
 *     notified until the chip is removed — simple, visible and explicit, and no
 *     fuzzy matching of edited text back to people);
 *   - removing a chip does NOT touch the text (it is just text again).
 *
 * ---------------------------------------------------------------------------
 * ACCESSIBILITY (ARIA 1.2 combobox, same contract as ui/ComboBox)
 * ---------------------------------------------------------------------------
 * The textarea carries `role="combobox"` with `aria-autocomplete="list"`,
 * `aria-expanded`, and — only while the list is open — `aria-controls` and
 * `aria-activedescendant`. DOM focus never leaves the textarea; the active
 * option is pointed at, not focused. The listbox is portaled (to the enclosing
 * Modal's popup root, else `document.body`) at `z-[70]` so a scroll container
 * cannot clip it. A persistent visually-hidden `role="status"` region announces
 * how many people the search found, and each pick / chip removal.
 *
 * Keyboard while the list is open: Up/Down move, Enter or Tab inserts the
 * active person, Escape closes the list ONLY (see the capture-phase listener
 * below for why that has to be a native window listener). Shift+Tab closes the
 * list and moves focus on. With the list closed every key behaves as in a plain
 * textarea (Enter is a newline); Ctrl/Cmd+Enter calls `onSubmitShortcut`.
 */

export interface MentionTextareaProps {
  /** Visible label (rendered through FormField). */
  label: React.ReactNode;
  value: string;
  onChange: (value: string) => void;
  /** The structured mention list — the people the server will notify. */
  mentions: CommentPerson[];
  onMentionsChange: (mentions: CommentPerson[]) => void;
  disabled?: boolean;
  /**
   * Read-only while a save is in flight. Unlike `disabled` it keeps keyboard
   * focus in the field (a disabled control drops focus to <body>).
   */
  readOnly?: boolean;
  placeholder?: string;
  rows?: number;
  /** Character cap for the counter / native maxLength. Defaults to the server's 5000. */
  maxLength?: number;
  /** Mention cap. Defaults to the server's 20. */
  maxMentions?: number;
  /** A caller-supplied field error (e.g. a refused mention). Shown under the field. */
  error?: string | null;
  required?: boolean;
  /** Ctrl/Cmd+Enter with the suggestion list closed. */
  onSubmitShortcut?: () => void;
  /** Override the generated textarea id. */
  id?: string;
  className?: string;
}

/** A mention trigger at the caret: the `@` index and the text typed after it. */
export interface MentionQuery {
  start: number;
  query: string;
}

// Letters (ASCII + Latin-1/Latin Extended-A/B, so accented names work without
// the `u` flag the es5 target cannot use), apostrophes, periods and hyphens,
// with at most ONE internal space so a full name ("@Jamie Ok") can be typed.
const NAME_CHARS = "A-Za-z\\u00C0-\\u024F'\\u2019.\\-";
const MENTION_AT_CARET = new RegExp(`(^|[\\s(])@([A-Za-z\\u00C0-\\u024F][${NAME_CHARS}]*(?: [${NAME_CHARS}]*)?)$`);

/** Max characters the server accepts for `q`. */
const QUERY_MAX_LENGTH = 100;
const SEARCH_LIMIT = 8;
const SEARCH_DEBOUNCE_MS = 200;

/**
 * The @-mention being typed at `caret`, or null. The `@` must start the text or
 * follow whitespace / an opening parenthesis, so an email address
 * (`a@example.com`) never opens the list.
 */
export function findMentionQuery(text: string, caret: number): MentionQuery | null {
  const before = text.slice(0, Math.max(0, Math.min(caret, text.length)));
  const match = MENTION_AT_CARET.exec(before);
  if (!match) return null;
  const query = match[2];
  return { start: before.length - query.length - 1, query };
}

type SearchState =
  | { status: 'idle' }
  | { status: 'loading'; query: string }
  | { status: 'ok'; query: string; people: CommentPerson[] }
  | { status: 'error'; query: string };

const OPTION_BASE =
  'flex w-full cursor-pointer items-center gap-2 px-3 py-1.5 text-left text-sm text-fd-body';

export const MentionTextarea = forwardRef<HTMLTextAreaElement, MentionTextareaProps>(function MentionTextarea(
  {
    label,
    value,
    onChange,
    mentions,
    onMentionsChange,
    disabled = false,
    readOnly = false,
    placeholder,
    rows = 3,
    maxLength = COMMENT_BODY_MAX_LENGTH,
    maxMentions = MAX_MENTIONS_PER_COMMENT,
    error = null,
    required = false,
    onSubmitShortcut,
    id,
    className = '',
  },
  forwardedRef
) {
  const modalPortal = useModalPortal();
  const generatedId = useId();
  const textareaId = id ?? `mention-textarea-${generatedId}`;
  const listId = `${textareaId}-mentions-listbox`;
  const optionId = (index: number) => `${textareaId}-mention-option-${index}`;

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(forwardedRef, () => textareaRef.current as HTMLTextAreaElement);
  const popupRef = useRef<HTMLDivElement>(null);
  const chipRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // Caret to restore after a pick rewrites the value (applied once the new
  // value has rendered, or the browser would put the caret at the end).
  const pendingCaretRef = useRef<number | null>(null);
  // Chip index to focus after one is removed (applied after the re-render).
  const pendingChipFocusRef = useRef<number | null>(null);
  const searchSeqRef = useRef(0);

  const [focused, setFocused] = useState(false);
  const [caret, setCaret] = useState(0);
  // The `@` index of a trigger the user closed (Escape) or just completed (a
  // pick). The same trigger stays closed while it is still being typed — so a
  // one-word name followed by the inserted space does not pop the list straight
  // back open — and a NEW `@` opens normally.
  const [dismissedStart, setDismissedStart] = useState<number | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [search, setSearch] = useState<SearchState>({ status: 'idle' });
  const [announcement, setAnnouncement] = useState('');
  const [limitError, setLimitError] = useState<string | null>(null);
  const [rect, setRect] = useState<{ top: number; left: number; width: number; flip: boolean } | null>(null);

  const locked = disabled || readOnly;
  const token = useMemo(() => (focused && !locked ? findMentionQuery(value, caret) : null), [
    focused,
    locked,
    value,
    caret,
  ]);

  // A trigger the user dismissed stops being "dismissed" once it is gone, so
  // typing a fresh `@` at the same index later opens again.
  // (Read straight from the text, not gated on focus, so a blur and refocus
  // does not undo an Escape.)
  useEffect(() => {
    if (dismissedStart === null) return;
    const current = findMentionQuery(value, caret);
    if (!current || current.start !== dismissedStart) setDismissedStart(null);
  }, [dismissedStart, value, caret]);

  const open = token !== null && token.start !== dismissedStart;
  const query = open && token ? token.query.trim().slice(0, QUERY_MAX_LENGTH) : '';
  const debouncedQuery = useDebouncedValue(query, SEARCH_DEBOUNCE_MS);
  const queryRef = useRef(query);
  queryRef.current = query;

  // The search. Keyed on the DEBOUNCED query (and on opening, so a list closed
  // and reopened faster than the debounce still searches); a debounced value the
  // user has already typed past is skipped, and a response that lands after a
  // newer request was sent is dropped (sequence guard).
  useEffect(() => {
    if (!debouncedQuery || debouncedQuery !== queryRef.current) return;
    const seq = ++searchSeqRef.current;
    setSearch(previous =>
      previous.status === 'ok' ? previous : { status: 'loading', query: debouncedQuery }
    );
    api
      .searchMentionableUsers(debouncedQuery, SEARCH_LIMIT)
      .then(people => {
        if (seq !== searchSeqRef.current) return;
        setSearch({ status: 'ok', query: debouncedQuery, people });
        setActiveIndex(0);
      })
      .catch(() => {
        if (seq !== searchSeqRef.current) return;
        setSearch({ status: 'error', query: debouncedQuery });
      });
  }, [debouncedQuery, open]);

  // Closing forgets the results, so the next `@` never flashes a stale list.
  // Opening drops the last action announcement, so closing the list again does
  // not make the live region repeat an old "Mentioned …".
  useEffect(() => {
    if (open) {
      setAnnouncement('');
      return;
    }
    searchSeqRef.current += 1;
    setSearch({ status: 'idle' });
    setActiveIndex(0);
  }, [open]);

  const people = search.status === 'ok' ? search.people : [];
  const pending = open && (search.status === 'loading' || (search.status === 'ok' && search.query !== query));
  // A two-word query with no match is far more likely prose ("@Ana said") than
  // a name, so it shows nothing rather than a "no matches" box.
  const showEmpty = open && search.status === 'ok' && people.length === 0 && !query.includes(' ');
  const showList = open && (people.length > 0 || showEmpty || search.status === 'error');
  const mentionedIds = useMemo(() => new Set(mentions.map(person => person.id)), [mentions]);

  // The live region. While the list is open it reports the search; otherwise
  // it carries the last action (a pick or a chip removal).
  const status = open
    ? search.status === 'error'
      ? 'Could not search people.'
      : search.status === 'ok' && !pending
        ? people.length === 0
          ? `No people match ${query}.`
          : `${people.length} ${people.length === 1 ? 'person' : 'people'} found. Use the up and down arrow keys to choose, then Enter to mention.`
        : query
          ? 'Searching people…'
          : ''
    : announcement;

  useEffect(() => {
    if (activeIndex > 0 && activeIndex >= people.length) setActiveIndex(Math.max(0, people.length - 1));
  }, [activeIndex, people.length]);

  // Anchor the portaled list under the textarea; re-measured on any scroll
  // (capture catches scroll containers) or resize while it is shown.
  const measure = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    const below = window.innerHeight - box.bottom;
    setRect({ top: box.bottom, left: box.left, width: box.width, flip: below < 220 && box.top > below });
  }, []);

  useLayoutEffect(() => {
    if (!showList) return;
    measure();
    window.addEventListener('scroll', measure, true);
    window.addEventListener('resize', measure);
    return () => {
      window.removeEventListener('scroll', measure, true);
      window.removeEventListener('resize', measure);
    };
  }, [showList, measure]);

  // ---------------------------------------------------------------------------
  // Escape closes the LIST ONLY — never a dialog behind it.
  // ---------------------------------------------------------------------------
  // Same reasoning as ui/ComboBox: `<Modal>` closes on a bubble-phase `window`
  // keydown and both portal to `document.body`, so a React `stopPropagation()`
  // on the textarea cannot be relied on to sit between them. A capture-phase
  // listener on `window` runs before any bubble-phase listener anywhere. It is
  // registered only while the list is open, and only acts on Escape.
  const listExpanded = showList && rect !== null;
  const openStart = listExpanded && token ? token.start : null;
  useEffect(() => {
    if (openStart === null) return;
    const onEscapeCapture = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      setDismissedStart(openStart);
    };
    window.addEventListener('keydown', onEscapeCapture, true);
    return () => window.removeEventListener('keydown', onEscapeCapture, true);
  }, [openStart]);

  useLayoutEffect(() => {
    const target = pendingCaretRef.current;
    if (target === null) return;
    pendingCaretRef.current = null;
    const el = textareaRef.current;
    if (!el) return;
    el.setSelectionRange(target, target);
    setCaret(target);
  }, [value]);

  useLayoutEffect(() => {
    const index = pendingChipFocusRef.current;
    if (index === null) return;
    pendingChipFocusRef.current = null;
    const chips = chipRefs.current.slice(0, mentions.length);
    const next = chips[Math.min(index, chips.length - 1)];
    if (next) next.focus();
    else textareaRef.current?.focus();
  }, [mentions]);

  const syncCaret = (el: HTMLTextAreaElement) => setCaret(el.selectionStart ?? el.value.length);

  const pick = (person: CommentPerson) => {
    if (!token) return;
    const before = value.slice(0, token.start);
    const after = value.slice(caret);
    const inserted = `@${person.display_name}`;
    const spacer = /^\s/.test(after) ? '' : ' ';
    const nextValue = `${before}${inserted}${spacer}${after}`;
    const alreadyMentioned = mentionedIds.has(person.id);

    if (!alreadyMentioned && mentions.length >= maxMentions) {
      const message = `You can mention at most ${maxMentions} people in one comment.`;
      setLimitError(message);
      setAnnouncement(message);
      setDismissedStart(token.start);
      return;
    }
    if (nextValue.length > maxLength) {
      const message = `No room for this mention: a comment can be at most ${maxLength} characters.`;
      setLimitError(message);
      setAnnouncement(message);
      setDismissedStart(token.start);
      return;
    }

    setLimitError(null);
    // Past the inserted space, or past the whitespace that already followed.
    pendingCaretRef.current = before.length + inserted.length + 1;
    setDismissedStart(token.start);
    onChange(nextValue);
    if (!alreadyMentioned) onMentionsChange([...mentions, person]);
    setAnnouncement(
      alreadyMentioned ? `${person.display_name} is already mentioned.` : `Mentioned ${person.display_name}.`
    );
  };

  const removeMention = (index: number) => {
    const person = mentions[index];
    if (!person) return;
    pendingChipFocusRef.current = index;
    setLimitError(null);
    onMentionsChange(mentions.filter((_, i) => i !== index));
    setAnnouncement(`Removed mention of ${person.display_name}.`);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const listActive = open && people.length > 0;
    if (listActive) {
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          setActiveIndex(i => Math.min(i + 1, people.length - 1));
          return;
        case 'ArrowUp':
          event.preventDefault();
          setActiveIndex(i => Math.max(i - 1, 0));
          return;
        case 'Enter':
          if (event.ctrlKey || event.metaKey || event.shiftKey) break;
          event.preventDefault();
          pick(people[Math.min(activeIndex, people.length - 1)]);
          return;
        case 'Tab':
          if (event.shiftKey) {
            if (token) setDismissedStart(token.start);
            return;
          }
          event.preventDefault();
          pick(people[Math.min(activeIndex, people.length - 1)]);
          return;
        // Escape is deliberately absent: the capture-phase window listener
        // above is the only place that can stop it before a Modal sees it.
        default:
          break;
      }
    }
    // A search still in flight (nothing shown yet): Escape cancels the trigger
    // but is NOT swallowed — with no visible list it belongs to whatever is
    // behind the field (an enclosing dialog closes as usual).
    if (event.key === 'Escape' && open && !listExpanded && token) {
      setDismissedStart(token.start);
    }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && onSubmitShortcut) {
      event.preventDefault();
      onSubmitShortcut();
    }
  };

  const trimmedLength = value.trim().length;
  const overLimit = trimmedLength > maxLength;
  const fieldError = error || limitError || (overLimit ? `A comment can be at most ${maxLength} characters.` : null);

  const popup =
    showList && rect ? (
      <div
        ref={popupRef}
        style={{
          position: 'fixed',
          top: rect.flip ? undefined : rect.top,
          bottom: rect.flip ? window.innerHeight - rect.top : undefined,
          left: rect.left,
          width: Math.min(rect.width, 352),
        }}
        className="z-[70] max-w-[calc(100vw-1rem)] border border-fd-line-bright bg-fd-panel shadow-xl"
      >
        <div id={listId} role="listbox" aria-label="People to mention" className="max-h-64 overflow-y-auto py-1">
          {search.status === 'error' && <p className="px-3 py-2 text-sm text-fd-red">Could not search people.</p>}
          {showEmpty && <p className="px-3 py-2 text-sm text-fd-faint">No matching people</p>}
          {people.map((person, index) => {
            const isActive = index === activeIndex;
            return (
              // A native <button> carrying role="option" (as in ui/ComboBox):
              // inherently clickable for the a11y rules, kept out of the tab
              // order, and mousedown is cancelled so a click never takes focus
              // off the textarea. `aria-selected` follows the active option
              // (APG list-autocomplete pattern: selection follows focus).
              <button
                key={person.id}
                type="button"
                tabIndex={-1}
                id={optionId(index)}
                role="option"
                aria-selected={isActive}
                onMouseDown={event => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => pick(person)}
                className={`${OPTION_BASE} ${isActive ? 'bg-fd-blue/15 text-fd-ink' : ''}`}
              >
                <span className="truncate">{person.display_name}</span>
                {mentionedIds.has(person.id) && (
                  <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-fd-faint">mentioned</span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    ) : null;

  return (
    <div className={className}>
      <FormField
        label={label}
        htmlFor={textareaId}
        required={required}
        error={fieldError}
        help={
          <span className="flex flex-wrap items-center justify-between gap-2">
            <span>Type @ and a name to mention someone.</span>
            <span className={`tabular-nums ${overLimit ? 'text-fd-red' : ''}`}>
              {trimmedLength} / {maxLength}
            </span>
          </span>
        }
      >
        {field => (
          <textarea
            {...field}
            ref={textareaRef}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={listExpanded}
            aria-controls={listExpanded ? listId : undefined}
            aria-activedescendant={listExpanded && people.length > 0 ? optionId(activeIndex) : undefined}
            rows={rows}
            maxLength={maxLength}
            value={value}
            disabled={disabled}
            readOnly={readOnly}
            placeholder={placeholder}
            onChange={event => {
              onChange(event.target.value);
              syncCaret(event.target);
              if (limitError) setLimitError(null);
            }}
            onSelect={event => syncCaret(event.currentTarget)}
            onClick={event => syncCaret(event.currentTarget)}
            onFocus={event => {
              setFocused(true);
              syncCaret(event.currentTarget);
            }}
            onBlur={() => setFocused(false)}
            onKeyDown={handleKeyDown}
            className="input w-full resize-y"
          />
        )}
      </FormField>

      <p className="sr-only" role="status" aria-live="polite">
        {status}
      </p>

      {mentions.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
            Notifying ({mentions.length}/{maxMentions})
          </span>
          <ul aria-label="Mentioned people" className="flex flex-wrap gap-1.5">
            {mentions.map((person, index) => (
              <li key={person.id}>
                <button
                  type="button"
                  ref={el => {
                    chipRefs.current[index] = el;
                  }}
                  aria-label={`Remove mention of ${person.display_name}`}
                  disabled={locked}
                  onClick={() => removeMention(index)}
                  className="inline-flex items-center gap-1 rounded-none border border-fd-blue/40 bg-fd-blue/10 px-1.5 py-0.5 text-xs text-fd-ink hover:border-fd-red/60 hover:bg-fd-red/10 disabled:opacity-50"
                >
                  @{person.display_name}
                  <XMarkIcon className="h-3 w-3 text-fd-mute" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {popup && createPortal(popup, modalPortal ?? document.body)}
    </div>
  );
});

export default MentionTextarea;
