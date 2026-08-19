import React, { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckIcon, ChevronUpDownIcon, XMarkIcon } from '@heroicons/react/20/solid';

/**
 * Searchable single-select — a type-ahead replacement for a long native
 * `<select>`, in the instrument-panel idiom.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS HAND-ROLLED
 * ---------------------------------------------------------------------------
 * `@headlessui/react` is already a dependency and ships a `Combobox`, so this
 * deliberately does not use it. Two reasons, both structural rather than
 * stylistic:
 *
 *  1. Its v2 popup positioning (`anchor`) runs on floating-ui, which needs
 *     `ResizeObserver`. `setupTests.ts` polyfills `matchMedia` and
 *     `IntersectionObserver` and NOT that one, so every suite rendering a
 *     picker would need a global polyfill added to make a UI control mount.
 *  2. The first caller puts one of these in every row of a table inside
 *     `overflow-auto` — a listbox rendered in the normal flow is clipped by the
 *     scroll container, so it has to be portaled and manually anchored either
 *     way. Owning the positioning is the smaller half of that job.
 *
 * The listbox portals to `document.body` at `z-[70]`, one layer above the
 * shared `<Modal>`'s `z-[60]`, so it renders above a dialog that contains it.
 *
 * ---------------------------------------------------------------------------
 * ACCESSIBILITY
 * ---------------------------------------------------------------------------
 * ARIA 1.2 combobox: the text input carries `role="combobox"` with
 * `aria-expanded` / `aria-controls` / `aria-activedescendant`, and the portaled
 * list carries `role="listbox"` with `role="option"` + `aria-selected` children.
 * Because the popup is a sibling of the input in the DOM (portal) rather than a
 * descendant, `aria-controls` and `aria-activedescendant` are what tie them
 * together — the visual focus ring stays on the input at all times and never
 * moves into the list, which is also what makes Escape/Tab behave.
 *
 * Keyboard: ArrowDown/ArrowUp move the active option (opening the list if
 * closed), Home/End jump to the ends, Enter commits the active option, Escape
 * closes and restores the committed label, Tab closes and lets focus move on.
 */

export interface ComboBoxOption {
  /** Stable value. `''` is reserved for the built-in "no selection" option. */
  value: string;
  /** Primary text — what the type-ahead matches against, with `hint`. */
  label: string;
  /** Secondary text shown dimmed after the label (e.g. stock on hand). */
  hint?: string;
  /** Optional section header. Options are rendered in the order given. */
  group?: string;
}

export interface ComboBoxProps {
  options: ComboBoxOption[];
  /** Committed value; `''` means nothing is selected. */
  value: string;
  onChange: (value: string) => void;
  /** Label for the built-in empty option. Omit to make a choice mandatory. */
  emptyOptionLabel?: string;
  placeholder?: string;
  disabled?: boolean;
  /** Applied to the trigger input, so callers control cell/field sizing. */
  className?: string;
  id?: string;
  ariaLabel?: string;
  ariaLabelledBy?: string;
  /**
   * Id of an element describing the control — a caller's inline notice or error
   * text. Kept off the accessible NAME (which stays `ariaLabel` / `ariaLabelledBy`)
   * so a long sentence is announced as a description, not as the field's name.
   */
  ariaDescribedBy?: string;
  /** Rendered pinned at the foot of the popup (e.g. a filter toggle). */
  footer?: React.ReactNode;
  /** Shown in place of the list when the query matches nothing. */
  noResultsLabel?: string;
}

const OPTION_BASE =
  'flex w-full cursor-pointer items-center gap-2 px-3 py-1.5 text-left text-sm text-fd-body';

/** Case- and whitespace-insensitive substring match over label + hint. */
function optionMatches(option: ComboBoxOption, needle: string): boolean {
  if (!needle) return true;
  const haystack = `${option.label} ${option.hint ?? ''}`.toLowerCase();
  // Every whitespace-separated term must appear, so "72x144 a36" narrows the
  // way a planner expects rather than being treated as one literal string.
  return needle
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}

export function ComboBox({
  options,
  value,
  onChange,
  emptyOptionLabel,
  placeholder,
  disabled = false,
  className = '',
  id,
  ariaLabel,
  ariaLabelledBy,
  ariaDescribedBy,
  footer,
  noResultsLabel = 'No matches',
}: ComboBoxProps) {
  const generatedId = useId();
  const inputId = id ?? `combobox-${generatedId}`;
  const listId = `${inputId}-listbox`;

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const [rect, setRect] = useState<{ top: number; left: number; width: number; flip: boolean } | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // Set for exactly one focus event, by `commit`'s focus restore. See there.
  const suppressFocusOpenRef = useRef(false);

  // The empty option is part of the list rather than a separate clear affordance
  // so keyboard users reach "none" the same way they reach anything else.
  const allOptions = useMemo<ComboBoxOption[]>(
    () => (emptyOptionLabel != null ? [{ value: '', label: emptyOptionLabel }, ...options] : options),
    [options, emptyOptionLabel]
  );

  const selected = useMemo(() => allOptions.find((option) => option.value === value), [allOptions, value]);
  const selectedLabel = value && selected ? selected.label : '';

  const filtered = useMemo(() => allOptions.filter((option) => optionMatches(option, query)), [allOptions, query]);

  // Anchor the portaled popup to the trigger. Recomputed on open and on any
  // scroll/resize — `true` (capture) is what catches the table's own scroll
  // container, which does not bubble a scroll event to the window.
  const measure = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    const below = window.innerHeight - box.bottom;
    setRect({
      top: box.bottom,
      left: box.left,
      width: box.width,
      // Flip above only when there is genuinely more room there, so the popup
      // does not jump upward for a control near the middle of a short viewport.
      flip: below < 220 && box.top > below,
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    measure();
    window.addEventListener('scroll', measure, true);
    window.addEventListener('resize', measure);
    return () => {
      window.removeEventListener('scroll', measure, true);
      window.removeEventListener('resize', measure);
    };
  }, [open, measure]);

  // Close on an outside pointer press. Both the trigger and the portaled popup
  // count as "inside" — they are far apart in the DOM but one control.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (inputRef.current?.contains(target) || popupRef.current?.contains(target)) return;
      setOpen(false);
      setQuery('');
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  // ---------------------------------------------------------------------------
  // Escape closes the POPUP ONLY — never the dialog behind it.
  // ---------------------------------------------------------------------------
  // This has to be a native capture-phase listener on `window`, not the React
  // `onKeyDown` below. `<Modal>` closes on a bubble-phase `window` keydown, and
  // both it and this popup portal to `document.body`, so a React
  // `stopPropagation()` on the input cannot be relied on to sit between the two
  // — measured: one Escape closed the popup AND the whole wizard, discarding
  // every row the planner had corrected. Capture at `window` runs before any
  // bubble-phase listener anywhere, so stopping propagation here is what makes
  // "Escape backs out one layer" true.
  //
  // Scoped as tightly as possible: only while the popup is open, and only for
  // Escape — every other key still reaches the input normally.
  useEffect(() => {
    if (!open) return;
    const onEscapeCapture = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      setOpen(false);
      setQuery('');
    };
    window.addEventListener('keydown', onEscapeCapture, true);
    return () => window.removeEventListener('keydown', onEscapeCapture, true);
  }, [open]);

  // Clamp the active option whenever the list shrinks under it.
  //
  // `activeIndex` indexes `filtered`, and `filtered` can shrink without a
  // keystroke — the caller re-filtering its own `options` is enough (the sheet
  // picker's "Show all materials" toggle does exactly that). Left unclamped,
  // `aria-activedescendant` points at an id that no longer exists and Enter
  // reads `filtered[activeIndex]` as `undefined`, so the control silently
  // refuses to commit and looks broken.
  useEffect(() => {
    if (filtered.length === 0) return;
    setActiveIndex((i) => Math.min(i, filtered.length - 1));
  }, [filtered.length]);

  // Keep the active option in view as the arrow keys walk past the fold.
  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector(`[data-index="${activeIndex}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [open, activeIndex]);

  const openList = () => {
    if (disabled) return;
    setOpen(true);
    const index = filtered.findIndex((option) => option.value === value);
    setActiveIndex(index >= 0 ? index : 0);
  };

  const commit = (option: ComboBoxOption | undefined) => {
    if (!option) return;
    onChange(option.value);
    setOpen(false);
    setQuery('');
    // A mouse click focuses the option button; committing unmounts it, and
    // focus would fall to <body>. Inside <Modal>'s Tab trap that restarts
    // tabbing at the top of the dialog — 40 rows from where the planner was.
    //
    // Guarded on both sides. The `activeElement` check means a KEYBOARD commit
    // (focus never left the input) does not call `focus()` at all, and the flag
    // means the MOUSE commit's focus event does not re-run `openList` and
    // immediately reopen the popup we just closed. Without it, committing by
    // click bounced straight back into an open, empty-query list.
    if (document.activeElement !== inputRef.current) {
      suppressFocusOpenRef.current = true;
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (!open) return openList();
        return setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
      case 'ArrowUp':
        event.preventDefault();
        if (!open) return openList();
        return setActiveIndex((i) => Math.max(i - 1, 0));
      case 'Home':
        if (!open) return;
        event.preventDefault();
        return setActiveIndex(0);
      case 'End':
        if (!open) return;
        event.preventDefault();
        return setActiveIndex(Math.max(0, filtered.length - 1));
      case 'Enter':
        if (!open) return;
        event.preventDefault();
        return commit(filtered[activeIndex]);
      // Escape is deliberately absent: it is handled by the capture-phase
      // window listener above, which is the only place that can stop it before
      // an enclosing <Modal>'s own window listener sees it.
      case 'Tab':
        setOpen(false);
        return setQuery('');
      default:
        return undefined;
    }
  };

  // Group headers are emitted as the group value changes, so callers control
  // ordering by ordering `options` and never by sorting here.
  let lastGroup: string | undefined;

  const popup = open && rect && (
    <div
      ref={popupRef}
      style={{
        position: 'fixed',
        top: rect.flip ? undefined : rect.top,
        bottom: rect.flip ? window.innerHeight - rect.top + (inputRef.current?.offsetHeight ?? 0) : undefined,
        left: rect.left,
        minWidth: rect.width,
      }}
      className="z-[70] max-w-[min(28rem,calc(100vw-1rem))] border border-fd-line-bright bg-fd-panel shadow-xl"
    >
      <div ref={listRef} id={listId} role="listbox" className="max-h-64 overflow-y-auto py-1">
        {filtered.length === 0 && <p className="px-3 py-2 text-sm text-fd-faint">{noResultsLabel}</p>}
        {filtered.map((option, index) => {
          const header = option.group && option.group !== lastGroup ? option.group : null;
          lastGroup = option.group;
          const isActive = index === activeIndex;
          const isSelected = option.value === value;
          return (
            <React.Fragment key={option.value || '__none__'}>
              {header && (
                <p className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-fd-faint">
                  {header}
                </p>
              )}
              {/* A native <button> carrying role="option": the ARIA role is what
                  the listbox needs, and the native element is what keeps the
                  a11y rules satisfied without a per-line suppression (it is
                  inherently clickable and focusable). `tabIndex={-1}` keeps it
                  out of the tab order — under ARIA 1.2 the combobox input holds
                  DOM focus at all times and points here via
                  `aria-activedescendant`. */}
              <button
                type="button"
                tabIndex={-1}
                id={`${inputId}-option-${index}`}
                data-index={index}
                role="option"
                aria-selected={isSelected}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => commit(option)}
                className={`${OPTION_BASE} ${isActive ? 'bg-fd-blue/15 text-fd-ink' : ''}`}
              >
                <CheckIcon
                  className={`h-3.5 w-3.5 shrink-0 text-fd-blue ${isSelected ? '' : 'invisible'}`}
                  aria-hidden="true"
                />
                <span className="truncate">{option.label}</span>
                {option.hint && <span className="ml-auto shrink-0 text-xs text-fd-faint">{option.hint}</span>}
              </button>
            </React.Fragment>
          );
        })}
      </div>
      {footer && <div className="border-t border-fd-line bg-fd-sunken px-3 py-1.5">{footer}</div>}
    </div>
  );

  return (
    <div className="relative">
      <input
        ref={inputRef}
        id={inputId}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-autocomplete="list"
        aria-activedescendant={open && filtered.length > 0 ? `${inputId}-option-${activeIndex}` : undefined}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-describedby={ariaDescribedBy}
        autoComplete="off"
        disabled={disabled}
        // Showing the committed label as the value (and the query only while
        // typing) keeps the cell readable at rest, which a table of 40 rows
        // needs more than it needs an always-empty search box.
        value={open ? query : selectedLabel}
        placeholder={placeholder}
        onChange={(e) => {
          setQuery(e.target.value);
          setActiveIndex(0);
          if (!open) setOpen(true);
        }}
        onFocus={() => {
          // Opening on focus keeps a tab-in from dead-ending on a control whose
          // choices are invisible. The one focus it must ignore is the one
          // `commit` fires to take focus back off a clicked option.
          if (suppressFocusOpenRef.current) {
            suppressFocusOpenRef.current = false;
            return;
          }
          openList();
        }}
        onClick={openList}
        onKeyDown={handleKeyDown}
        className={`w-full rounded-none border border-fd-line bg-fd-sunken py-1 pl-2 pr-12 text-sm text-fd-ink placeholder:text-fd-faint focus:border-fd-blue focus:outline-none disabled:opacity-50 ${className}`}
      />
      {/* Clear is offered only when clearing is a legal choice. */}
      {emptyOptionLabel != null && value !== '' && !disabled && (
        <button
          type="button"
          tabIndex={-1}
          aria-label="Clear selection"
          onClick={() => {
            onChange('');
            setQuery('');
            inputRef.current?.focus();
          }}
          className="absolute right-6 top-1/2 -translate-y-1/2 text-fd-mute hover:text-fd-red"
        >
          <XMarkIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      <ChevronUpDownIcon
        className="pointer-events-none absolute right-1.5 top-1/2 h-4 w-4 -translate-y-1/2 text-fd-mute"
        aria-hidden="true"
      />
      {popup && createPortal(popup, document.body)}
    </div>
  );
}

export default ComboBox;
