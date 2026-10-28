import React, { useEffect, useId, useRef, useState } from 'react';
import api from '../../services/api';
import { getAccessToken } from '../../services/realtime';
import { workspaceIdentity } from '../../hooks/useWorkspaceRecords';
import type { CustomerPOSuggestion, CustomerPOSuggestionsResponse } from '../../types/customerPurchaseOrders';

interface CustomerPOAutocompleteProps extends Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  'value' | 'onChange' | 'onSelect'
> {
  value: string;
  customerName?: string | null;
  onChange: (value: string) => void;
  onSelect: (suggestion: CustomerPOSuggestion) => void;
}

/** Free-text PO entry with exact customer/PO suggestions from existing work orders. */
export default function CustomerPOAutocomplete({
  value,
  customerName,
  onChange,
  onSelect,
  ...inputProps
}: CustomerPOAutocompleteProps) {
  const autoId = useId();
  const listId = `${autoId}-po-options`;
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [, authChanged] = useState(0);
  const identity = workspaceIdentity();
  const token = getAccessToken();
  const search = value.trim();
  // A blank customer allows picking a customer/PO pair. Once named, restrict
  // suggestions to that exact customer; never silently substitute another one.
  const customerFilter = customerName?.trim() ? customerName : undefined;
  const scope = JSON.stringify([identity, token, value, customerFilter]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const sequence = useRef(0);
  const activeOption = useRef<HTMLButtonElement | null>(null);
  const [state, setState] = useState<{
    scope: string;
    loading: boolean;
    error: boolean;
    data?: CustomerPOSuggestionsResponse;
  }>({ scope, loading: false, error: false });

  useEffect(() => {
    const changed = () => authChanged(current => current + 1);
    window.addEventListener('werco:auth-token-changed', changed);
    window.addEventListener('storage', changed);
    return () => {
      window.removeEventListener('werco:auth-token-changed', changed);
      window.removeEventListener('storage', changed);
    };
  }, []);

  useEffect(() => {
    const seq = ++sequence.current;
    if (!open || !search || inputProps.disabled) return;
    const controller = new AbortController();
    setState({ scope, loading: true, error: false });
    const timer = window.setTimeout(async () => {
      const isCurrent = () =>
        !controller.signal.aborted &&
        sequence.current === seq &&
        currentScope.current === scope &&
        workspaceIdentity() === identity &&
        getAccessToken() === token;
      try {
        const data = await api.getCustomerPOSuggestions(
          {
            search,
            ...(customerFilter === undefined ? {} : { customer_name: customerFilter }),
            limit: 10,
          },
          controller.signal
        );
        if (isCurrent()) setState({ scope, loading: false, error: false, data });
      } catch {
        if (isCurrent()) setState({ scope, loading: false, error: true });
      }
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      ++sequence.current;
    };
  }, [open, search, customerFilter, identity, token, scope, inputProps.disabled]);

  useEffect(() => {
    setActiveIndex(-1);
  }, [scope]);

  const visible = state.scope === scope ? state : { scope, loading: true, error: false, data: undefined };
  const items = visible.data?.items ?? [];
  const expanded = open && Boolean(search) && !inputProps.disabled;
  const select = (suggestion: CustomerPOSuggestion) => {
    if (currentScope.current !== scope || workspaceIdentity() !== identity || getAccessToken() !== token) return;
    onChange(suggestion.customer_po);
    onSelect(suggestion);
    setOpen(false);
    setActiveIndex(-1);
  };
  const highlighted = expanded && activeIndex >= 0 && activeIndex < items.length ? activeIndex : -1;

  useEffect(() => {
    activeOption.current?.scrollIntoView?.({ block: 'nearest' });
  }, [highlighted]);

  return (
    <div className="relative min-w-0">
      <input
        {...inputProps}
        type="text"
        role="combobox"
        autoComplete="off"
        aria-autocomplete="list"
        aria-expanded={expanded}
        aria-controls={expanded ? listId : undefined}
        aria-activedescendant={highlighted >= 0 ? `${listId}-${highlighted}` : undefined}
        className={inputProps.className || 'input'}
        value={value}
        onFocus={event => {
          setOpen(true);
          inputProps.onFocus?.(event);
        }}
        onBlur={event => {
          setOpen(false);
          setActiveIndex(-1);
          inputProps.onBlur?.(event);
        }}
        onChange={event => {
          setOpen(true);
          onChange(event.target.value);
        }}
        onKeyDown={event => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            setOpen(true);
            if (items.length)
              setActiveIndex(current =>
                event.key === 'ArrowDown' ? (current + 1) % items.length : current <= 0 ? items.length - 1 : current - 1
              );
          } else if (event.key === 'Enter' && highlighted >= 0) {
            event.preventDefault();
            event.stopPropagation();
            select(items[highlighted]);
          } else if (event.key === 'Escape' && open) {
            event.preventDefault();
            event.stopPropagation();
            setOpen(false);
            setActiveIndex(-1);
          }
          inputProps.onKeyDown?.(event);
        }}
      />
      {expanded && (
        <div className="absolute z-30 mt-1 w-full overflow-hidden rounded border border-slate-600 bg-slate-900 shadow-lg">
          <ul
            id={listId}
            role="listbox"
            aria-label="Existing customer purchase orders"
            className="max-h-60 overflow-y-auto"
          >
            {items.map((suggestion, index) => (
              <li key={JSON.stringify([suggestion.customer_name, suggestion.customer_po])} role="presentation">
                <button
                  type="button"
                  role="option"
                  id={`${listId}-${index}`}
                  aria-selected={highlighted === index}
                  tabIndex={-1}
                  ref={highlighted === index ? activeOption : null}
                  className={`w-full px-3 py-2 text-left text-sm [overflow-wrap:anywhere] ${highlighted === index ? 'bg-blue-500/20 text-blue-200' : 'text-slate-200 hover:bg-slate-800'}`}
                  onMouseDown={event => event.preventDefault()}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => select(suggestion)}
                >
                  <span className="block font-mono">{suggestion.customer_po}</span>
                  <span className="block text-xs text-slate-400">
                    {suggestion.customer_name || 'No customer assigned'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {visible.loading && (
            <p role="status" className="px-3 py-2 text-xs text-slate-400">
              Finding purchase orders…
            </p>
          )}
          {visible.error && (
            <p role="status" className="px-3 py-2 text-xs text-amber-300">
              Suggestions unavailable. You can still enter a PO number.
            </p>
          )}
          {!visible.loading && !visible.error && !items.length && (
            <p role="status" className="px-3 py-2 text-xs text-slate-400">
              No matching POs. You can use this as a new PO number.
            </p>
          )}
          {visible.data?.has_more && (
            <p className="border-t border-slate-700 px-3 py-2 text-xs text-slate-400">
              Keep typing to narrow the matches.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
