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
};

// Type labels and colors
const typeConfig: Record<string, { label: string; color: string }> = {
  part: { label: 'Part', color: 'bg-blue-100 text-blue-700 border border-blue-200' },
  work_order: { label: 'Work Order', color: 'bg-cyan-100 text-cyan-700 border border-cyan-200' },
  customer: { label: 'Customer', color: 'bg-green-100 text-green-700 border border-green-200' },
  bom: { label: 'BOM', color: 'bg-purple-100 text-purple-700 border border-purple-200' },
  routing: { label: 'Routing', color: 'bg-orange-100 text-orange-700 border border-orange-200' },
  user: { label: 'User', color: 'bg-pink-100 text-pink-700 border border-pink-200' },
  vendor: { label: 'Vendor', color: 'bg-yellow-100 text-yellow-700 border border-yellow-200' },
  purchase_order: { label: 'PO', color: 'bg-red-100 text-red-700 border border-red-200' },
  quote: { label: 'Quote', color: 'bg-emerald-100 text-emerald-700 border border-emerald-200' },
  inventory: { label: 'Inventory', color: 'bg-indigo-100 text-indigo-700 border border-indigo-200' },
};

// Navigation items for quick access
const quickActions = [
  { name: 'Traceability', url: '/traceability', icon: DocumentDuplicateIcon },
  { name: 'Purchasing', url: '/purchasing', icon: TruckIcon },
  { name: 'Customers', url: '/customers', icon: BuildingOfficeIcon },
  { name: 'Quotes', url: '/quotes', icon: CurrencyDollarIcon },
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
  const debounceRef = useRef<NodeJS.Timeout>();
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
      const data = await api.search(searchQuery);
      if (seq === searchSeqRef.current) {
        setResults(data.results || []);
        setSelectedIndex(0);
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
    const items = query ? results : recentItems;

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

  const displayResults = query ? results : recentItems;
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
          <div className="fixed inset-0 bg-slate-900/60 backdrop-blur-sm" />
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
            <Dialog.Panel className="mx-auto max-w-2xl transform overflow-hidden rounded-2xl bg-base-100 shadow-2xl ring-1 ring-base-300 transition-all">
              {/* Search Input */}
              <div className="relative">
                <MagnifyingGlassIcon className="pointer-events-none absolute left-4 top-4.5 h-5 w-5 text-base-content/40" />
                <input
                  ref={inputRef}
                  type="text"
                  className="du-input du-input-ghost w-full h-14 rounded-none pl-12 pr-12 text-lg text-base-content placeholder:text-base-content/50 focus:outline-none"
                  placeholder="Search across your workspace..."
                  value={query}
                  onChange={e => setQuery(e.target.value)}
                  onKeyDown={handleKeyDown}
                />
                {query && (
                  <button
                    onClick={() => setQuery('')}
                    className="du-btn du-btn-ghost du-btn-sm du-btn-circle absolute right-3 top-3.5"
                  >
                    <XMarkIcon className="h-5 w-5" />
                  </button>
                )}
              </div>

              {/* Results */}
              <div className="max-h-[60vh] overflow-y-auto border-t border-base-300">
                {isLoading && (
                  <div className="py-8 text-center">
                    <span className="du-loading du-loading-spinner du-loading-md text-primary" />
                  </div>
                )}

                {!isLoading && displayResults.length > 0 && (
                  <div className="py-2">
                    {/* Section Header */}
                    <div className="px-4 py-2 flex items-center gap-2 text-xs font-medium text-base-content/50 uppercase tracking-wider">
                      <ClockIcon className="h-4 w-4" />
                      {query ? 'Results' : 'Recent'}
                    </div>

                    {/* Results List */}
                    <ul>
                      {displayResults.map((result, index) => {
                        const IconComponent = iconMap[result.icon] || CubeIcon;
                        const config = typeConfig[result.type] || {
                          label: result.type,
                          color: 'bg-base-200 text-base-content/70 border border-base-300',
                        };
                        const isSelected = index === selectedIndex;

                        return (
                          <li
                            key={`${result.type}-${result.id}`}
                            className={`
                              px-4 py-3 cursor-pointer flex items-center gap-3 transition-colors
                              ${isSelected ? 'bg-base-200' : 'hover:bg-base-200/60'}
                            `}
                            onClick={() => handleSelect(result)}
                            onMouseEnter={() => setSelectedIndex(index)}
                          >
                            <div className={`p-2 rounded-lg ${config.color}`}>
                              <IconComponent className="h-5 w-5" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="font-medium text-base-content truncate">{result.title}</span>
                                <span className={`du-badge du-badge-sm ${config.color}`}>{config.label}</span>
                              </div>
                              {result.subtitle && (
                                <p className="text-sm text-base-content/60 truncate">{result.subtitle}</p>
                              )}
                            </div>
                            {isSelected && <kbd className="du-kbd du-kbd-sm hidden sm:inline-flex">Enter</kbd>}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}

                {/* Quick Actions when no results */}
                {!isLoading && showQuickActions && (
                  <div className="py-2">
                    <div className="px-4 py-2 text-xs font-medium text-base-content/50 uppercase tracking-wider">
                      Quick Actions
                    </div>
                    <ul>
                      {quickActions.map(action => (
                        <li
                          key={action.url}
                          className="px-4 py-3 cursor-pointer flex items-center gap-3 hover:bg-base-200/60 transition-colors"
                          onClick={() => handleSelect(action)}
                        >
                          <action.icon className="h-5 w-5 text-base-content/50" />
                          <span className="text-base-content">{action.name}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* No Results */}
                {!isLoading && query && results.length === 0 && (
                  <div className="py-12 text-center">
                    <MagnifyingGlassIcon className="mx-auto h-10 w-10 text-base-content/40" />
                    <p className="mt-3 text-base-content/70">No results found for "{query}"</p>
                    <p className="text-sm text-base-content/50 mt-1">
                      Try searching for parts, work orders, customers, or quotes
                    </p>
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between gap-4 px-4 py-3 border-t border-base-300 text-xs text-base-content/50">
                <div className="flex items-center gap-4">
                  <span className="flex items-center gap-1">
                    <kbd className="du-kbd du-kbd-sm">Up/Down</kbd>
                    Navigate
                  </span>
                  <span className="flex items-center gap-1">
                    <kbd className="du-kbd du-kbd-sm">Enter</kbd>
                    Select
                  </span>
                  <span className="flex items-center gap-1">
                    <kbd className="du-kbd du-kbd-sm">Esc</kbd>
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
