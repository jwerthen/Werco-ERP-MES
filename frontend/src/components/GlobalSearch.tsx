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
  part: { label: 'Part', color: 'bg-blue-500/20 text-blue-400' },
  work_order: { label: 'Work Order', color: 'bg-cyan-500/20 text-cyan-400' },
  customer: { label: 'Customer', color: 'bg-green-500/20 text-green-400' },
  bom: { label: 'BOM', color: 'bg-purple-500/20 text-purple-400' },
  routing: { label: 'Routing', color: 'bg-orange-500/20 text-orange-400' },
  user: { label: 'User', color: 'bg-pink-500/20 text-pink-400' },
  vendor: { label: 'Vendor', color: 'bg-yellow-500/20 text-yellow-400' },
  purchase_order: { label: 'PO', color: 'bg-red-500/20 text-red-400' },
  quote: { label: 'Quote', color: 'bg-emerald-500/20 text-emerald-400' },
  inventory: { label: 'Inventory', color: 'bg-indigo-500/20 text-indigo-400' },
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
          <div className="fixed inset-0 bg-gray-900/80 backdrop-blur-sm" />
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
            <Dialog.Panel className="mx-auto max-w-2xl transform overflow-hidden rounded-2xl bg-gray-800 shadow-2xl ring-1 ring-white/10 transition-all">
              {/* Search Input */}
              <div className="relative">
                <MagnifyingGlassIcon className="pointer-events-none absolute left-4 top-4 h-5 w-5 text-gray-400" />
                <input
                  ref={inputRef}
                  type="text"
                  className="w-full border-0 bg-transparent pl-12 pr-12 py-4 text-white placeholder:text-gray-400 focus:ring-0 text-lg"
                  placeholder="Search across your workspace..."
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={handleKeyDown}
                />
                {query && (
                  <button
                    onClick={() => setQuery('')}
                    className="absolute right-4 top-4 text-gray-400 hover:text-white"
                  >
                    <XMarkIcon className="h-5 w-5" />
                  </button>
                )}
              </div>

              {/* Results */}
              <div className="max-h-[60vh] overflow-y-auto border-t border-gray-700">
                {isLoading && (
                  <div className="py-8 text-center">
                    <div className="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-cyan-500"></div>
                  </div>
                )}

                {!isLoading && displayResults.length > 0 && (
                  <div className="py-2">
                    {/* Section Header */}
                    <div className="px-4 py-2 flex items-center gap-2 text-xs font-medium text-gray-400 uppercase tracking-wider">
                      <ClockIcon className="h-4 w-4" />
                      {query ? 'Results' : 'Recent'}
                    </div>
                    
                    {/* Results List */}
                    <ul>
                      {displayResults.map((result, index) => {
                        const IconComponent = iconMap[result.icon] || CubeIcon;
                        const config = typeConfig[result.type] || { label: result.type, color: 'bg-gray-500/20 text-gray-400' };
                        const isSelected = index === selectedIndex;
                        
                        return (
                          <li
                            key={`${result.type}-${result.id}`}
                            className={`
                              px-4 py-3 cursor-pointer flex items-center gap-3
                              ${isSelected ? 'bg-white/10' : 'hover:bg-white/5'}
                            `}
                            onClick={() => handleSelect(result)}
                            onMouseEnter={() => setSelectedIndex(index)}
                          >
                            <div className={`p-2 rounded-lg ${config.color}`}>
                              <IconComponent className="h-5 w-5" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="font-medium text-white truncate">
                                  {result.title}
                                </span>
                                <span className={`text-xs px-2 py-0.5 rounded-full ${config.color}`}>
                                  {config.label}
                                </span>
                              </div>
                              {result.subtitle && (
                                <p className="text-sm text-gray-400 truncate">
                                  {result.subtitle}
                                </p>
                              )}
                            </div>
                            {isSelected && (
                              <kbd className="hidden sm:inline-flex items-center gap-1 px-2 py-1 text-xs text-gray-400 bg-gray-700 rounded">
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
                    <div className="px-4 py-2 text-xs font-medium text-gray-400 uppercase tracking-wider">
                      Quick Actions
                    </div>
                    <ul>
                      {quickActions.map((action) => (
                        <li
                          key={action.url}
                          className="px-4 py-3 cursor-pointer flex items-center gap-3 hover:bg-white/5"
                          onClick={() => handleSelect(action)}
                        >
                          <action.icon className="h-5 w-5 text-gray-400" />
                          <span className="text-white">{action.name}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* No Results */}
                {!isLoading && query && results.length === 0 && (
                  <div className="py-12 text-center">
                    <MagnifyingGlassIcon className="mx-auto h-10 w-10 text-gray-500" />
                    <p className="mt-3 text-gray-400">
                      No results found for "{query}"
                    </p>
                    <p className="text-sm text-gray-500 mt-1">
                      Try searching for parts, work orders, customers, or quotes
                    </p>
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between gap-4 px-4 py-3 border-t border-gray-700 text-xs text-gray-500">
                <div className="flex items-center gap-4">
                  <span className="flex items-center gap-1">
                    <kbd className="px-1.5 py-0.5 bg-gray-700 rounded">Up/Down</kbd>
                    Navigate
                  </span>
                  <span className="flex items-center gap-1">
                    <kbd className="px-1.5 py-0.5 bg-gray-700 rounded">Enter</kbd>
                    Select
                  </span>
                  <span className="flex items-center gap-1">
                    <kbd className="px-1.5 py-0.5 bg-gray-700 rounded">esc</kbd>
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
