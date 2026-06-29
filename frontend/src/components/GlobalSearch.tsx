/**
 * Global Search Component (Command Palette)
 *
 * A Spotlight/Command-K style search dialog that searches across
 * all entities in the system.
 *
 * Features:
 * - Keyboard shortcut (Cmd/Ctrl+K)
 * - Real-time search with debouncing
 * - Categorized results
 * - Recent items when no query
 * - Keyboard navigation
 */

import React, { useState, useEffect, useRef, useCallback, Fragment } from 'react';
import { useNavigate } from 'react-router-dom';
import { Dialog, Transition } from '@headlessui/react';
import {
  MagnifyingGlassIcon,
  CubeIcon,
  ClipboardDocumentListIcon,
  BuildingOfficeIcon,
  DocumentDuplicateIcon,
  ListBulletIcon,
  UserIcon,
  TruckIcon,
  CurrencyDollarIcon,
  ClockIcon,
  XMarkIcon,
  ArchiveBoxIcon,
  ArrowUpTrayIcon,
  PlusCircleIcon,
  RocketLaunchIcon,
  BellAlertIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';

interface SearchResult {
  id: number;
  type: string;
  title: string;
  subtitle?: string;
  url: string;
  icon: string;
}

// Icon mapping
const iconMap: Record<string, React.ComponentType<{ className?: string }>> = {
  cube: CubeIcon,
  clipboard: ClipboardDocumentListIcon,
  building: BuildingOfficeIcon,
  document: DocumentDuplicateIcon,
  list: ListBulletIcon,
  user: UserIcon,
  truck: TruckIcon,
  currency: CurrencyDollarIcon,
  archive: ArchiveBoxIcon,
  upload: ArrowUpTrayIcon,
  plus: PlusCircleIcon,
  rocket: RocketLaunchIcon,
  bell: BellAlertIcon,
};

// Type labels and colors
const typeConfig: Record<string, { label: string; color: string }> = {
  part: { label: 'Part', color: 'bg-blue-500/20 text-blue-300 border border-blue-500/30' },
  work_order: { label: 'Work Order', color: 'bg-blue-500/20 text-blue-300 border border-blue-500/30' },
  customer: { label: 'Customer', color: 'bg-green-500/20 text-green-300 border border-green-500/30' },
  bom: { label: 'BOM', color: 'bg-purple-500/20 text-purple-300 border border-purple-500/30' },
  routing: { label: 'Routing', color: 'bg-orange-500/20 text-orange-300 border border-orange-500/30' },
  user: { label: 'User', color: 'bg-pink-500/20 text-pink-300 border border-pink-500/30' },
  vendor: { label: 'Vendor', color: 'bg-yellow-500/20 text-yellow-300 border border-yellow-500/30' },
  purchase_order: { label: 'PO', color: 'bg-red-500/20 text-red-300 border border-red-500/30' },
  quote: { label: 'Quote', color: 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' },
  inventory: { label: 'Inventory', color: 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30' },
  action: { label: 'Action', color: 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30' },
};

// Navigation items for quick access
const quickActions = [
  { name: 'Action Inbox', url: '/action-inbox', icon: BellAlertIcon },
  { name: 'Setup Wizard', url: '/setup', icon: RocketLaunchIcon },
  { name: 'Import Center', url: '/import-center', icon: ArrowUpTrayIcon },
  { name: 'Traceability', url: '/traceability', icon: DocumentDuplicateIcon },
  { name: 'Purchasing', url: '/purchasing', icon: TruckIcon },
  { name: 'Customers', url: '/customers', icon: BuildingOfficeIcon },
  { name: 'Quotes', url: '/quotes', icon: CurrencyDollarIcon },
];

const commandActions: SearchResult[] = [
  {
    id: -6,
    type: 'action',
    title: 'Open Action Inbox',
    subtitle: 'Review setup gaps, master-data issues, and notification activity',
    url: '/action-inbox',
    icon: 'bell',
  },
  {
    id: -1,
    type: 'action',
    title: 'Open Setup Wizard',
    subtitle: 'Onboarding checklist, master-data health, and readiness gaps',
    url: '/setup',
    icon: 'rocket',
  },
  {
    id: -2,
    type: 'action',
    title: 'Import Employees',
    subtitle: 'Mass upload an employee list from CSV',
    url: '/import-center?type=employees',
    icon: 'upload',
  },
  {
    id: -3,
    type: 'action',
    title: 'Import Parts',
    subtitle: 'Download the parts template and continue import prep',
    url: '/import-center?type=parts',
    icon: 'upload',
  },
  {
    id: -4,
    type: 'action',
    title: 'Create Work Order',
    subtitle: 'Start a new work order after readiness checks',
    url: '/work-orders/new',
    icon: 'plus',
  },
  {
    id: -5,
    type: 'action',
    title: 'Generate Routings',
    subtitle: 'Create routings for top-level make parts only',
    url: '/routing',
    icon: 'list',
  },
];

interface GlobalSearchProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function GlobalSearch({ isOpen, onClose }: GlobalSearchProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [recentItems, setRecentItems] = useState<SearchResult[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const recentFetchRef = useRef<number>(0);
  const searchSeqRef = useRef(0);

  // Fetch recent items when dialog opens
  useEffect(() => {
    if (isOpen) {
      const now = Date.now();
      if (!recentItems.length || now - recentFetchRef.current > 60000) {
        recentFetchRef.current = now;
        fetchRecentItems();
      }
      setQuery('');
      setResults([]);
      setSelectedIndex(0);
      // Focus input after a short delay to allow animation
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [isOpen, recentItems.length]);

  const fetchRecentItems = async () => {
    try {
      const data = await api.getRecentItems();
      setRecentItems(data);
    } catch (error) {
      console.error('Failed to fetch recent items:', error);
    }
  };

  // Debounced search
  const search = useCallback(async (searchQuery: string) => {
    if (!searchQuery.trim()) {
      setResults([]);
      return;
    }

    setIsLoading(true);
    const seq = ++searchSeqRef.current;
    try {
      const shouldTryNaturalLanguage =
        /\s/.test(searchQuery.trim()) &&
        /(late|overdue|waiting|material|blocked|stuck|hold|laser|weld|brake|hot|rush|critical)/i.test(searchQuery);
      const data = shouldTryNaturalLanguage
        ? await api.naturalLanguageSearch(searchQuery)
        : await api.search(searchQuery);
      if (seq === searchSeqRef.current) {
        const nextResults = data.results || [];
        setResults(nextResults);
        setSelectedIndex(0);
        if (nextResults.length === 0) {
          window.dispatchEvent(
            new CustomEvent('werco:friction', { detail: { type: 'failed_search', query: searchQuery.trim() } })
          );
        }
      }
    } catch (error) {
      console.error('Search failed:', error);
      if (seq === searchSeqRef.current) {
        setResults([]);
      }
    } finally {
      if (seq === searchSeqRef.current) {
        setIsLoading(false);
      }
    }
  }, []);

  // Handle query change with debouncing
  useEffect(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }

    debounceRef.current = setTimeout(() => {
      search(query);
    }, 200);

    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, [query, search]);

  // Handle result selection
  const handleSelect = (result: SearchResult | { url: string }) => {
    onClose();
    navigate(result.url);
  };

  // Keyboard navigation
  const handleKeyDown = (e: React.KeyboardEvent) => {
    const items = query ? displayResults : recentItems;

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        setSelectedIndex(prev => Math.min(prev + 1, items.length - 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        setSelectedIndex(prev => Math.max(prev - 1, 0));
        break;
      case 'Enter':
        e.preventDefault();
        if (items[selectedIndex]) {
          handleSelect(items[selectedIndex]);
        }
        break;
      case 'Escape':
        onClose();
        break;
    }
  };

  const normalizedQuery = query.trim().toLowerCase();
  const matchingCommandActions = normalizedQuery
    ? commandActions.filter((action) =>
        `${action.title} ${action.subtitle || ''}`.toLowerCase().includes(normalizedQuery)
      )
    : [];
  const displayResults = query ? [...matchingCommandActions, ...results] : recentItems;
  const showQuickActions = !query && recentItems.length === 0;

  return (
    <Transition.Root show={isOpen} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={onClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-200"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-150"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-slate-950/70 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 z-10 overflow-y-auto p-4 sm:p-6 md:p-20">
          <Transition.Child
            as={Fragment}
            enter="ease-out duration-200"
            enterFrom="opacity-0 scale-95"
            enterTo="opacity-100 scale-100"
            leave="ease-in duration-150"
            leaveFrom="opacity-100 scale-100"
            leaveTo="opacity-0 scale-95"
          >
            <Dialog.Panel
              className="mx-auto max-w-2xl transform overflow-hidden rounded-sm shadow-2xl transition-all"
              style={{ background: 'var(--fd-panel)', border: '1px solid var(--fd-line-bright)' }}
            >
              {/* Search Input */}
              <div className="relative">
                <MagnifyingGlassIcon className="pointer-events-none absolute left-4 top-4.5 h-5 w-5 text-fd-mute" />
                <input
                  ref={inputRef}
                  type="text"
                  className="w-full h-14 bg-transparent pl-12 pr-12 text-lg text-fd-ink placeholder:text-fd-faint focus:outline-none"
                  placeholder="Search across your workspace..."
                  value={query}
                  onChange={e => setQuery(e.target.value)}
                  onKeyDown={handleKeyDown}
                />
                {query && (
                  <button
                    onClick={() => setQuery('')}
                    className="absolute right-3 top-3.5 p-1.5 rounded-[3px] text-fd-mute hover:text-fd-ink hover:bg-white/5 transition-colors"
                    aria-label="Clear search"
                  >
                    <XMarkIcon className="h-5 w-5" />
                  </button>
                )}
              </div>

              {/* Results */}
              <div className="max-h-[60vh] overflow-y-auto" style={{ borderTop: '1px solid var(--fd-line)' }}>
                {isLoading && (
                  <div className="py-8 text-center">
                    <span className="du-loading du-loading-spinner du-loading-md text-fd-blue" />
                  </div>
                )}

                {!isLoading && displayResults.length > 0 && (
                  <div className="py-2">
                    {/* Section Header */}
                    <div className="px-4 py-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fd-faint">
                      <ClockIcon className="h-3.5 w-3.5" />
                      {query ? 'Results' : 'Recent'}
                    </div>

                    {/* Results List */}
                    <ul>
                      {displayResults.map((result, index) => {
                        const IconComponent = iconMap[result.icon] || CubeIcon;
                        const config = typeConfig[result.type] || {
                          label: result.type,
                          color: 'bg-fd-sunken text-fd-body border border-fd-line',
                        };
                        const isSelected = index === selectedIndex;

                        return (
                          <li
                            key={`${result.type}-${result.id}`}
                            className={`
                              px-4 py-3 cursor-pointer flex items-center gap-3 transition-colors
                              ${isSelected ? 'bg-white/[0.04] shadow-[inset_2px_0_0_#2f81f7]' : 'hover:bg-white/[0.02]'}
                            `}
                            onClick={() => handleSelect(result)}
                            onMouseEnter={() => setSelectedIndex(index)}
                          >
                            <div className={`p-2 rounded-[3px] ${config.color}`}>
                              <IconComponent className="h-5 w-5" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="font-medium text-fd-ink truncate">{result.title}</span>
                                <span className={`inline-flex px-1.5 py-0.5 rounded-[3px] text-[10px] font-mono uppercase tracking-wide ${config.color}`}>{config.label}</span>
                              </div>
                              {result.subtitle && (
                                <p className="text-sm text-fd-mute truncate">{result.subtitle}</p>
                              )}
                            </div>
                            {isSelected && (
                              <kbd className="hidden sm:inline-flex items-center px-1.5 py-0.5 font-mono text-[10px] text-fd-faint rounded-[3px]" style={{ border: '1px solid var(--fd-line)' }}>
                                Enter
                              </kbd>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}

                {/* Quick Actions when no results */}
                {!isLoading && showQuickActions && (
                  <div className="py-2">
                    <div className="px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fd-faint">
                      Quick Actions
                    </div>
                    <ul>
                      {quickActions.map(action => (
                        <li
                          key={action.url}
                          className="px-4 py-3 cursor-pointer flex items-center gap-3 hover:bg-white/[0.02] transition-colors"
                          onClick={() => handleSelect(action)}
                        >
                          <action.icon className="h-5 w-5 text-fd-mute" />
                          <span className="text-fd-body">{action.name}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* No Results */}
                {!isLoading && query && displayResults.length === 0 && (
                  <div className="py-12 text-center">
                    <MagnifyingGlassIcon className="mx-auto h-10 w-10 text-fd-faint" />
                    <p className="mt-3 text-fd-body">No results found for "{query}"</p>
                    <p className="text-sm text-fd-mute mt-1">
                      Try searching for parts, work orders, customers, or quotes
                    </p>
                  </div>
                )}
              </div>

              {/* Footer */}
              <div
                className="flex items-center justify-between gap-4 px-4 py-3 font-mono text-[11px] text-fd-mute"
                style={{ borderTop: '1px solid var(--fd-line)', background: 'var(--fd-sunken)' }}
              >
                <div className="flex items-center gap-4">
                  <span className="flex items-center gap-1.5">
                    <kbd className="inline-flex items-center px-1.5 py-0.5 rounded-[3px] text-fd-faint" style={{ border: '1px solid var(--fd-line)' }}>Up/Down</kbd>
                    Navigate
                  </span>
                  <span className="flex items-center gap-1.5">
                    <kbd className="inline-flex items-center px-1.5 py-0.5 rounded-[3px] text-fd-faint" style={{ border: '1px solid var(--fd-line)' }}>Enter</kbd>
                    Select
                  </span>
                  <span className="flex items-center gap-1.5">
                    <kbd className="inline-flex items-center px-1.5 py-0.5 rounded-[3px] text-fd-faint" style={{ border: '1px solid var(--fd-line)' }}>Esc</kbd>
                    Close
                  </span>
                </div>
              </div>
            </Dialog.Panel>
          </Transition.Child>
        </div>
      </Dialog>
    </Transition.Root>
  );
}

/**
 * Hook to open global search with keyboard shortcut
 */
export function useGlobalSearch() {
  const [isOpen, setIsOpen] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Cmd/Ctrl + K
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setIsOpen(true);
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  return {
    isOpen,
    open: () => setIsOpen(true),
    close: () => setIsOpen(false),
  };
}
