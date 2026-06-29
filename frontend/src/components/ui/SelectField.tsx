import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckIcon, ChevronUpDownIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline';

export interface SelectOption<TValue extends string | number = string | number> {
  value: TValue;
  label: string;
  description?: string;
  disabled?: boolean;
}

interface SelectFieldProps<TValue extends string | number = string | number> {
  value: TValue;
  options: SelectOption<TValue>[];
  onChange: (value: TValue) => void;
  placeholder?: string;
  disabled?: boolean;
  searchable?: boolean;
  className?: string;
  buttonClassName?: string;
  menuClassName?: string;
  ariaLabel?: string;
}

export function SelectField<TValue extends string | number = string | number>({
  value,
  options,
  onChange,
  placeholder = 'Select...',
  disabled = false,
  searchable = false,
  className = '',
  buttonClassName = '',
  menuClassName = '',
  ariaLabel,
}: SelectFieldProps<TValue>) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const selectedOption = options.find((option) => option.value === value);

  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return options;

    return options.filter((option) => (
      `${option.label} ${option.description || ''}`.toLowerCase().includes(normalized)
    ));
  }, [options, query]);

  useEffect(() => {
    if (!open) return;

    const buttonRect = buttonRef.current?.getBoundingClientRect();
    if (buttonRect) {
      setMenuStyle({
        position: 'fixed',
        top: buttonRect.bottom + 4,
        left: buttonRect.left,
        width: buttonRect.width,
      });
    }

    const selectedIndex = filteredOptions.findIndex((option) => option.value === value);
    setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : 0);

    if (searchable) {
      window.setTimeout(() => searchRef.current?.focus(), 0);
    }
  }, [open, filteredOptions, searchable, value]);

  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        !containerRef.current?.contains(target)
        && !menuRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };

    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, []);

  useEffect(() => {
    if (!open) return;

    const updateMenuPosition = () => {
      const buttonRect = buttonRef.current?.getBoundingClientRect();
      if (!buttonRect) return;
      setMenuStyle({
        position: 'fixed',
        top: buttonRect.bottom + 4,
        left: buttonRect.left,
        width: buttonRect.width,
      });
    };

    window.addEventListener('resize', updateMenuPosition);
    window.addEventListener('scroll', updateMenuPosition, true);
    return () => {
      window.removeEventListener('resize', updateMenuPosition);
      window.removeEventListener('scroll', updateMenuPosition, true);
    };
  }, [open]);

  const selectOption = (option: SelectOption<TValue>) => {
    if (option.disabled) return;
    onChange(option.value);
    setOpen(false);
    setQuery('');
  };

  const moveHighlight = (direction: 1 | -1) => {
    if (filteredOptions.length === 0) return;

    setHighlightedIndex((current) => {
      let next = current;
      for (let i = 0; i < filteredOptions.length; i += 1) {
        next = (next + direction + filteredOptions.length) % filteredOptions.length;
        if (!filteredOptions[next]?.disabled) return next;
      }
      return current;
    });
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (disabled) return;

    if (!open && ['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
      event.preventDefault();
      setOpen(true);
      return;
    }

    if (!open) return;

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveHighlight(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveHighlight(-1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const option = filteredOptions[highlightedIndex];
      if (option) selectOption(option);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
      setQuery('');
    }
  };

  return (
    <div ref={containerRef} className={`relative ${className}`} onKeyDown={handleKeyDown}>
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        onClick={() => {
          if (!disabled) setOpen((current) => !current);
        }}
        className={`input flex items-center justify-between gap-3 text-left ${buttonClassName}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
      >
        <span className={`min-w-0 truncate ${selectedOption ? 'text-slate-100' : 'text-slate-500'}`}>
          {selectedOption?.label || placeholder}
        </span>
        <ChevronUpDownIcon className="h-5 w-5 shrink-0 text-slate-500" />
      </button>

      {open && createPortal((
        <div
          ref={menuRef}
          style={menuStyle}
          className={`z-50 overflow-hidden rounded-xl border border-slate-700 bg-fd-panel shadow-2xl shadow-black/40 ${menuClassName}`}
        >
          {searchable && (
            <div className="relative border-b border-slate-700/70">
              <MagnifyingGlassIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
              <input
                ref={searchRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="w-full bg-slate-950/40 py-2.5 pl-9 pr-3 text-sm text-slate-100 outline-none placeholder:text-slate-500"
                placeholder="Search..."
              />
            </div>
          )}

          <div className="max-h-72 overflow-y-auto py-1" role="listbox">
            {filteredOptions.length > 0 ? (
              filteredOptions.map((option, index) => {
                const selected = option.value === value;
                const highlighted = index === highlightedIndex;
                return (
                  <button
                    key={`${option.value}`}
                    type="button"
                    disabled={option.disabled}
                    onMouseEnter={() => setHighlightedIndex(index)}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      selectOption(option);
                    }}
                    className={`flex w-full items-start gap-3 px-3 py-2.5 text-left text-sm transition-colors ${
                      highlighted ? 'bg-cyan-500/12 text-white' : 'text-slate-200 hover:bg-slate-800/80'
                    } ${option.disabled ? 'cursor-not-allowed opacity-50' : ''}`}
                    role="option"
                    aria-selected={selected}
                  >
                    <span className="mt-0.5 h-4 w-4 shrink-0">
                      {selected && <CheckIcon className="h-4 w-4 text-cyan-300" />}
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{option.label}</span>
                      {option.description && (
                        <span className="mt-0.5 block truncate text-xs text-slate-400">{option.description}</span>
                      )}
                    </span>
                  </button>
                );
              })
            ) : (
              <div className="px-3 py-3 text-sm text-slate-400">No matches found</div>
            )}
          </div>
        </div>
      ), document.body)}
    </div>
  );
}
