import React, { useRef } from 'react';

export interface Tab {
  id: string;
  label: string;
  icon?: React.ComponentType<{ className?: string }>;
  badge?: string | number;
  /** The caller owns role="tabpanel" with this ID and aria-labelledby={`${panelId}-tab`}. */
  panelId?: string;
}

interface TabsProps {
  tabs: Tab[];
  activeTab: string;
  onChange: (tabId: string) => void;
}

export function Tabs({ tabs, activeTab, onChange }: TabsProps) {
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  return (
    <div className="border-b border-slate-700">
      <div className="-mb-px flex space-x-6 overflow-x-auto" role="tablist" aria-label="Tabs">
        {tabs.map((tab, index) => {
          const isActive = tab.id === activeTab;
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              type="button"
              id={tab.panelId ? `${tab.panelId}-tab` : undefined}
              role="tab"
              aria-selected={isActive}
              aria-controls={tab.panelId}
              tabIndex={isActive ? 0 : -1}
              ref={node => {
                buttons.current[index] = node;
              }}
              onKeyDown={event => {
                const next =
                  event.key === 'ArrowRight'
                    ? (index + 1) % tabs.length
                    : event.key === 'ArrowLeft'
                      ? (index - 1 + tabs.length) % tabs.length
                      : event.key === 'Home'
                        ? 0
                        : event.key === 'End'
                          ? tabs.length - 1
                          : null;
                if (next === null) return;
                event.preventDefault();
                onChange(tabs[next].id);
                buttons.current[next]?.focus();
              }}
              onClick={() => onChange(tab.id)}
              className={`whitespace-nowrap py-3 px-1 border-b-2 text-sm font-medium transition-colors flex items-center gap-2 ${
                isActive
                  ? 'border-werco-navy-600 text-blue-300'
                  : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-500'
              }`}
            >
              {Icon && <Icon className="h-4 w-4" />}
              {tab.label}
              {tab.badge !== undefined && (
                <span
                  className={`inline-flex items-center justify-center px-2 py-0.5 rounded-full text-xs font-medium ${
                    isActive ? 'bg-werco-navy-600/20 text-werco-navy-300' : 'bg-slate-700 text-slate-400'
                  }`}
                >
                  {tab.badge}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
