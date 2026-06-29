import React, { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { Part } from '../types';
import { BOM, Routing } from '../types/engineering';
import { Breadcrumbs } from '../components/ui/Breadcrumbs';
import { getBreadcrumbParent } from '../utils/routeMeta';
import { Tabs, Tab } from '../components/ui/Tabs';
import { StatusBadge } from '../components/ui/StatusBadge';
import { Button, statusVariant, type StatusVariant } from '../components/ui';
import { useToast } from '../components/ui/Toast';
import { PartOverviewTab } from '../components/parts/PartOverviewTab';
import { PartBOMTab } from '../components/parts/PartBOMTab';
import { PartRoutingTab } from '../components/parts/PartRoutingTab';
import { partTypeColors } from '../types/engineering';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import {
  CubeIcon,
  ListBulletIcon,
  WrenchScrewdriverIcon,
  PencilIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  CurrencyDollarIcon,
  ClipboardDocumentCheckIcon,
} from '@heroicons/react/24/outline';

const TABS: Tab[] = [
  { id: 'overview', label: 'Overview', icon: CubeIcon },
  { id: 'bom', label: 'Bill of Materials', icon: ListBulletIcon },
  { id: 'routing', label: 'Routing', icon: WrenchScrewdriverIcon },
];

interface PartReadiness {
  ready: boolean;
  blockers: string[];
  warnings: string[];
  checks: Record<string, string>;
}

// Map a BOM/Routing status to an instrument-panel value color (text-only, for
// MiniStat values). Derived from the central status variant so it can't drift
// from the canonical status coloring used by StatusBadge.
const variantValueColor: Record<StatusVariant, string> = {
  green: 'text-fd-green',
  blue: 'text-fd-blue',
  amber: 'text-fd-amber',
  red: 'text-fd-red',
  slate: 'text-slate-400',
};

function statusValueColor(status: string): string {
  return variantValueColor[statusVariant(status)];
}

export default function PartDetail() {
  const { id } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [part, setPart] = useState<Part | null>(null);
  const [bom, setBom] = useState<BOM | null>(null);
  const [routing, setRouting] = useState<Routing | null>(null);
  const [readiness, setReadiness] = useState<PartReadiness | null>(null);
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

  const loadReadiness = useCallback(async () => {
    try {
      const data = await api.getPartReadiness(partId);
      setReadiness(data);
    } catch {
      setReadiness(null);
    }
  }, [partId]);

  useEffect(() => {
    async function load() {
      setLoading(true);
      await Promise.all([loadPart(), loadBOM(), loadRouting(), loadReadiness()]);
      setLoading(false);
    }
    load();
  }, [loadPart, loadBOM, loadRouting, loadReadiness]);

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
      <div className="space-y-4 animate-pulse">
        <div className="h-6 w-48 bg-fd-sunken rounded-sm" />
        <div className="h-10 w-72 bg-fd-sunken rounded-sm" />
        <div className="h-8 w-full bg-fd-sunken rounded-sm" />
        <div className="h-64 bg-fd-sunken rounded-sm" />
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
      {/* Breadcrumbs — Parts › {part number} */}
      <Breadcrumbs crumbs={[
        getBreadcrumbParent(`/parts/${part.id}`) ?? { label: 'Parts', href: '/parts' },
        { label: part.part_number },
      ]} />

      {/* Part Header */}
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-2xl font-bold text-white truncate tabular-nums">{part.part_number}</h1>
            <span className={`inline-flex px-2 py-0.5 rounded-sm text-xs font-semibold ${partTypeColors[part.part_type] || 'bg-fd-sunken text-slate-100'}`}>
              {part.part_type.replace('_', ' ')}
            </span>
            <StatusBadge status={part.status} />
            {part.is_critical && (
              <span className="inline-flex px-2 py-0.5 rounded-sm bg-fd-red/15 text-fd-red text-xs font-medium">
                Critical
              </span>
            )}
          </div>
          <p className="text-slate-400 mt-1 truncate">{part.name}</p>
          <div className="flex items-center gap-4 text-sm text-slate-400 mt-1">
            <span className="tabular-nums">Rev {part.revision}</span>
            {part.customer_name && <span className="min-w-0 truncate">Customer: {part.customer_name}</span>}
            {part.drawing_number && <span className="min-w-0 truncate tabular-nums">Dwg: {part.drawing_number}</span>}
          </div>
        </div>
        <Button
          variant="secondary"
          onClick={() => navigate(`/parts/${part.id}/edit`)}
          className="flex items-center gap-2 shrink-0"
        >
          <PencilIcon className="h-4 w-4" />
          Edit Part
        </Button>
      </div>

      {/* Quick Stats */}
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <MiniStat
          icon={CurrencyDollarIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Standard Cost"
          value={`$${Number(part.standard_cost || 0).toFixed(2)}`}
        />
        <MiniStat
          icon={ListBulletIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="BOM Status"
          value={bom ? bom.status.replace(/_/g, ' ') : 'None'}
          valueColor={bom ? statusValueColor(bom.status) : 'text-slate-500'}
        />
        <MiniStat
          icon={WrenchScrewdriverIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Routing Status"
          value={routing ? routing.status.replace(/_/g, ' ') : 'None'}
          valueColor={routing ? statusValueColor(routing.status) : 'text-slate-500'}
        />
        <MiniStat
          icon={ClipboardDocumentCheckIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Inspection"
          value={part.requires_inspection ? 'Required' : 'Not required'}
        />
      </MiniStatStrip>

      {/* Readiness — compact chip row */}
      {readiness && (readiness.blockers.length > 0 || readiness.warnings.length > 0) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {[...readiness.blockers, ...readiness.warnings].map((message) => (
            <span
              key={message}
              className="inline-flex items-center gap-1.5 rounded-sm border border-fd-amber/30 bg-fd-amber/10 px-2 py-1 text-xs text-fd-amber"
            >
              <ExclamationTriangleIcon className="h-3.5 w-3.5 flex-shrink-0" />
              <span className="min-w-0 max-w-xs truncate">{message}</span>
            </span>
          ))}
        </div>
      )}

      {readiness?.ready && readiness.warnings.length === 0 && (
        <div className="inline-flex items-center gap-1.5 rounded-sm border border-fd-green/30 bg-fd-green/10 px-2 py-1 text-xs text-fd-green">
          <CheckCircleIcon className="h-3.5 w-3.5 flex-shrink-0" />
          This part has the required active production data for its type.
        </div>
      )}

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
