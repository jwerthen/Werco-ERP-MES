import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  ArchiveBoxIcon,
  InboxArrowDownIcon,
  PaperAirplaneIcon,
} from '@heroicons/react/24/outline';

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
  const [activeTab, setActiveTab] = useState<WarehouseTab>(() => {
    const tab = searchParams.get('tab');
    if (tab === 'receiving' || tab === 'shipping') return tab;
    return 'inventory';
  });

  const handleTabChange = (tab: WarehouseTab) => {
    setActiveTab(tab);
    const nextParams = new URLSearchParams();
    nextParams.set('tab', tab);
    setSearchParams(nextParams, { replace: true });
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-white">Warehouse</h1>
        <p className="text-sm text-slate-400 mt-1">
          Inventory, receiving, and shipping &mdash; all in one place
        </p>
      </div>

      {/* Tab Navigation */}
      <div className="border-b border-slate-700">
        <nav className="-mb-px flex space-x-1 sm:space-x-6 overflow-x-auto">
          {tabs.map((tab) => {
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => handleTabChange(tab.id)}
                className={`group flex items-center gap-2 whitespace-nowrap px-3 sm:px-4 py-3 border-b-2 font-medium text-sm transition-all ${
                  isActive
                    ? 'border-werco-primary text-werco-primary'
                    : 'border-transparent text-slate-400 hover:text-slate-300 hover:border-slate-600'
                }`}
              >
                <tab.icon className={`h-5 w-5 flex-shrink-0 ${isActive ? 'text-werco-primary' : 'text-slate-400 group-hover:text-slate-400'}`} />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </nav>
      </div>

      {/* Tab Content */}
      <div>
        {activeTab === 'inventory' && <InventoryPage embedded />}
        {activeTab === 'receiving' && <ReceivingPage embedded />}
        {activeTab === 'shipping' && <ShippingPage embedded />}
      </div>
    </div>
  );
}
