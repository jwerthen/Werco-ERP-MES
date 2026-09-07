import { PageHeader } from '../components/ui/PageHeader';
import React from 'react';
import { tabKeyboard } from '../components/operations/tabKeyboard';
import { useSearchParams } from 'react-router-dom';
import { ArchiveBoxIcon, InboxArrowDownIcon, PaperAirplaneIcon } from '@heroicons/react/24/outline';

import InventoryPage from './Inventory';
import ReceivingPage from './Receiving';
import ShippingPage from './Shipping';

type WarehouseTab = 'inventory' | 'receiving' | 'shipping';

const tabs = [
  { id: 'inventory' as const, label: 'Inventory', icon: ArchiveBoxIcon },
  { id: 'receiving' as const, label: 'Receiving & Inspection', icon: InboxArrowDownIcon },
  { id: 'shipping' as const, label: 'Shipping', icon: PaperAirplaneIcon },
];

export default function Warehouse() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get('tab');
  const activeTab: WarehouseTab =
    requestedTab === 'receiving' || requestedTab === 'shipping' ? requestedTab : 'inventory';

  const handleTabChange = (tab: WarehouseTab) => {
    if (activeTab === tab) return;
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('tab', tab);
    setSearchParams(nextParams);
  };

  return (
    <div className="space-y-6">
      <PageHeader title="Warehouse" description="Inventory, receiving, and shipping in one place" />

      {/* Tab Navigation */}
      <div className="border-b border-slate-700">
        <div
          tabIndex={-1}
          role="tablist"
          aria-label="Warehouse sections"
          onKeyDown={tabKeyboard}
          className="-mb-px flex space-x-1 sm:space-x-6 overflow-x-auto"
        >
          {tabs.map(tab => {
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                role="tab"
                id={`warehouse-tab-${tab.id}`}
                aria-controls="warehouse-panel"
                aria-selected={activeTab === tab.id}
                tabIndex={activeTab === tab.id ? 0 : -1}
                onClick={() => handleTabChange(tab.id)}
                className={`group flex items-center gap-2 whitespace-nowrap px-3 sm:px-4 py-3 border-b-2 font-medium text-sm transition-all ${
                  isActive
                    ? 'border-werco-primary text-werco-primary'
                    : 'border-transparent text-slate-400 hover:text-slate-300 hover:border-slate-600'
                }`}
              >
                <tab.icon
                  className={`h-5 w-5 flex-shrink-0 ${isActive ? 'text-werco-primary' : 'text-slate-400 group-hover:text-slate-400'}`}
                />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Tab Content */}
      <div role="tabpanel" id="warehouse-panel" aria-labelledby={`warehouse-tab-${activeTab}`}>
        {activeTab === 'inventory' && <InventoryPage embedded />}
        {activeTab === 'receiving' && <ReceivingPage embedded />}
        {activeTab === 'shipping' && <ShippingPage embedded />}
      </div>
    </div>
  );
}
