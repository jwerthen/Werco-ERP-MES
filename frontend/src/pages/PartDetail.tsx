import React, { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { Part } from '../types';
import { BOM, Routing } from '../types/engineering';
import { Breadcrumbs } from '../components/ui/Breadcrumbs';
import { Tabs, Tab } from '../components/ui/Tabs';
import { StatusBadge } from '../components/ui/StatusBadge';
import { useToast } from '../components/ui/Toast';
import { PartOverviewTab } from '../components/parts/PartOverviewTab';
import { PartBOMTab } from '../components/parts/PartBOMTab';
import { PartRoutingTab } from '../components/parts/PartRoutingTab';
import { partTypeColors } from '../types/engineering';
import {
  CubeIcon,
  ListBulletIcon,
  WrenchScrewdriverIcon,
  PencilIcon,
} from '@heroicons/react/24/outline';

const TABS: Tab[] = [
  { id: 'overview', label: 'Overview', icon: CubeIcon },
  { id: 'bom', label: 'Bill of Materials', icon: ListBulletIcon },
  { id: 'routing', label: 'Routing', icon: WrenchScrewdriverIcon },
];

export default function PartDetail() {
  const { id } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [part, setPart] = useState<Part | null>(null);
  const [bom, setBom] = useState<BOM | null>(null);
  const [routing, setRouting] = useState<Routing | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') || 'overview');

  const partId = Number(id);

  const loadPart = useCallback(async () => {
    try {
      const data = await api.getPart(partId);
      setPart(data);
    } catch {
      showToast('error', 'Failed to load part');
      navigate('/parts');
    }
  }, [partId, navigate, showToast]);

  const loadBOM = useCallback(async () => {
    try {
      const data = await api.getBOMByPart(partId);
      setBom(data);
    } catch {
      setBom(null);
    }
  }, [partId]);

  const loadRouting = useCallback(async () => {
    try {
      const data = await api.getRoutingByPart(partId);
      setRouting(data);
    } catch {
      setRouting(null);
    }
  }, [partId]);

  useEffect(() => {
    async function load() {
      setLoading(true);
      await Promise.all([loadPart(), loadBOM(), loadRouting()]);
      setLoading(false);
    }
    load();
  }, [loadPart, loadBOM, loadRouting]);

  const handleTabChange = (tabId: string) => {
    setActiveTab(tabId);
    setSearchParams({ tab: tabId }, { replace: true });
  };

  const handlePartUpdated = (updated: Part) => {
    setPart(updated);
    showToast('success', 'Part updated');
  };

  if (loading) {
    return (
      <div className="space-y-6 animate-pulse">
        <div className="h-6 w-48 bg-slate-700 rounded" />
        <div className="h-10 w-72 bg-slate-700 rounded" />
        <div className="h-8 w-full bg-slate-700 rounded" />
        <div className="h-64 bg-slate-700 rounded-xl" />
      </div>
    );
  }

  if (!part) return null;

  const tabs: Tab[] = TABS.map(tab => {
    if (tab.id === 'bom' && bom) return { ...tab, badge: bom.items.length };
    if (tab.id === 'routing' && routing) return { ...tab, badge: routing.operations.length };
    return tab;
  });

  return (
    <div className="space-y-4">
      {/* Breadcrumbs */}
      <Breadcrumbs crumbs={[
        { label: 'Parts', href: '/parts' },
        { label: part.part_number },
      ]} />

      {/* Part Header */}
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-2xl font-bold text-white truncate">{part.part_number}</h1>
            <span className={`inline-flex px-2.5 py-1 rounded-md text-xs font-semibold ${partTypeColors[part.part_type] || 'bg-slate-800 text-slate-100'}`}>
              {part.part_type.replace('_', ' ')}
            </span>
            <StatusBadge status={part.status} />
            {part.is_critical && (
              <span className="inline-flex px-2 py-0.5 rounded bg-red-500/20 text-red-300 text-xs font-medium">
                Critical
              </span>
            )}
          </div>
          <p className="text-slate-400 mt-1">{part.name}</p>
          <div className="flex items-center gap-4 text-sm text-slate-400 mt-1">
            <span>Rev {part.revision}</span>
            {part.customer_name && <span>Customer: {part.customer_name}</span>}
            {part.drawing_number && <span>Dwg: {part.drawing_number}</span>}
          </div>
        </div>
        <button
          onClick={() => navigate(`/parts/${part.id}/edit`)}
          className="btn-secondary flex items-center gap-2 shrink-0"
        >
          <PencilIcon className="h-4 w-4" />
          Edit Part
        </button>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-[#151b28] border border-slate-700 rounded-lg p-3">
          <div className="text-xs text-slate-400 uppercase tracking-wide">Standard Cost</div>
          <div className="text-lg font-semibold mt-0.5">${Number(part.standard_cost || 0).toFixed(2)}</div>
        </div>
        <div className="bg-[#151b28] border border-slate-700 rounded-lg p-3">
          <div className="text-xs text-slate-400 uppercase tracking-wide">BOM Status</div>
          <div className="mt-0.5">
            {bom ? <StatusBadge status={bom.status} /> : <span className="text-sm text-slate-500">None</span>}
          </div>
        </div>
        <div className="bg-[#151b28] border border-slate-700 rounded-lg p-3">
          <div className="text-xs text-slate-400 uppercase tracking-wide">Routing Status</div>
          <div className="mt-0.5">
            {routing ? <StatusBadge status={routing.status} /> : <span className="text-sm text-slate-500">None</span>}
          </div>
        </div>
        <div className="bg-[#151b28] border border-slate-700 rounded-lg p-3">
          <div className="text-xs text-slate-400 uppercase tracking-wide">Inspection</div>
          <div className="text-sm font-medium mt-0.5">
            {part.requires_inspection ? 'Required' : 'Not required'}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <Tabs tabs={tabs} activeTab={activeTab} onChange={handleTabChange} />

      {/* Tab Content */}
      <div className="mt-2">
        {activeTab === 'overview' && (
          <PartOverviewTab part={part} onPartUpdated={handlePartUpdated} />
        )}
        {activeTab === 'bom' && (
          <PartBOMTab
            part={part}
            bom={bom}
            onBOMChanged={loadBOM}
          />
        )}
        {activeTab === 'routing' && (
          <PartRoutingTab
            part={part}
            routing={routing}
            onRoutingChanged={loadRouting}
          />
        )}
      </div>
    </div>
  );
}
