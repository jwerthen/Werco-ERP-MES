import React from 'react';

export interface Tab {
  id: string;
  label: string;
  icon?: React.ComponentType<{ className?: string }>;
  badge?: string | number;
}

interface TabsProps {
  tabs: Tab[];
  activeTab: string;
  onChange: (tabId: string) => void;
}

export function Tabs({ tabs, activeTab, onChange }: TabsProps) {
  return (
    <div className="border-b border-gray-200">
      <nav className="-mb-px flex space-x-6 overflow-x-auto" aria-label="Tabs">
        {tabs.map(tab => {
          const isActive = tab.id === activeTab;
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              onClick={() => onChange(tab.id)}
              className={`whitespace-nowrap py-3 px-1 border-b-2 text-sm font-medium transition-colors flex items-center gap-2 ${
                isActive
                  ? 'border-werco-navy-600 text-werco-navy-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              }`}
            >
              {Icon && <Icon className="h-4 w-4" />}
              {tab.label}
              {tab.badge !== undefined && (
                <span className={`inline-flex items-center justify-center px-2 py-0.5 rounded-full text-xs font-medium ${
                  isActive ? 'bg-werco-navy-100 text-werco-navy-700' : 'bg-gray-100 text-gray-600'
                }`}>
                  {tab.badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
