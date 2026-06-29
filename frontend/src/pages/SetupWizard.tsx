import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { EmptyState, ErrorState } from '../components/ui';
import {
  ArrowPathIcon,
  ArrowRightIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PlayCircleIcon,
  RocketLaunchIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';

interface SetupStep {
  key: string;
  label: string;
  status: 'complete' | 'missing';
  count: number;
  required_count: number;
  href: string;
  reason?: string;
}

interface MasterDataIssue {
  key: string;
  severity: 'high' | 'medium' | 'low';
  title: string;
  detail: string;
  count: number;
  href: string;
}

interface SetupHealth {
  progress: number;
  counts: Record<string, number>;
  steps: SetupStep[];
  issues: MasterDataIssue[];
}

const fallbackSteps: SetupStep[] = [
  { key: 'employees', label: 'Employees imported', status: 'missing', count: 0, required_count: 1, href: '/import-center?type=employees', reason: 'Import or add employees.' },
  { key: 'work_centers', label: 'Work centers configured', status: 'missing', count: 0, required_count: 1, href: '/work-centers', reason: 'Create at least one work center.' },
  { key: 'parts', label: 'Parts loaded', status: 'missing', count: 0, required_count: 1, href: '/import-center?type=parts', reason: 'Import or create parts.' },
  { key: 'boms', label: 'BOMs created', status: 'missing', count: 0, required_count: 1, href: '/import-center?type=boms', reason: 'Import or create BOMs.' },
  { key: 'routings', label: 'Routings created', status: 'missing', count: 0, required_count: 1, href: '/routing', reason: 'Create or generate routings.' },
  { key: 'work_orders', label: 'First work order', status: 'missing', count: 0, required_count: 1, href: '/work-orders/new', reason: 'Create your first work order.' },
];

export default function SetupWizard() {
  const [health, setHealth] = useState<SetupHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const loadHealth = async () => {
    setLoading(true);
    try {
      const data = await api.getSetupHealth();
      setHealth(data);
      setLoadError(false);
    } catch (err) {
      console.error('Failed to load setup health:', err);
      setHealth({ progress: 0, counts: {}, steps: fallbackSteps, issues: [] });
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadHealth();
  }, []);

  const steps = health?.steps || fallbackSteps;
  const currentStep = useMemo(() => steps.find((step) => step.status !== 'complete') || steps[steps.length - 1], [steps]);
  const blockingIssues = health?.issues?.filter((issue) => issue.severity === 'high') || [];
  const issueCount = (keys: string[]) =>
    health?.issues
      ?.filter((issue) => keys.includes(issue.key))
      .reduce((total, issue) => total + issue.count, 0) || 0;
  const reviewQueue = [
    {
      key: 'imports',
      title: 'Import review',
      detail: 'Load employees, parts, customers, vendors, and work centers from one place before production starts.',
      href: '/import-center',
      count: steps.filter((step) => step.status !== 'complete' && ['employees', 'parts', 'work_centers'].includes(step.key)).length,
    },
    {
      key: 'bom',
      title: 'BOM review',
      detail: 'Find assemblies without released BOMs and keep component parts under their assembly tree.',
      href: '/bom',
      count: issueCount(['assemblies_without_bom', 'draft_boms', 'inactive_bom_components']),
    },
    {
      key: 'routing',
      title: 'Routing review',
      detail: 'Generate or release routings only for top-level make parts, not BOM component rows.',
      href: '/routing',
      count: issueCount(['top_level_parts_without_routing', 'draft_routings', 'inactive_routing_work_centers']),
    },
    {
      key: 'readiness',
      title: 'Work order readiness',
      detail: 'Use part and work-order readiness checks to explain missing BOM, routing, and work-center data before release.',
      href: '/work-orders/new',
      count: blockingIssues.length,
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <RocketLaunchIcon className="h-8 w-8 text-cyan-300" />
            <h1 className="text-2xl font-bold text-white">Setup Wizard</h1>
          </div>
          <p className="text-slate-400 mt-1">Load the minimum master data needed to run your first clean job.</p>
        </div>
        <button onClick={loadHealth} className="btn-secondary flex items-center" disabled={loading}>
          <ArrowPathIcon className={`h-5 w-5 mr-2 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {loadError && (
        <ErrorState
          message="Could not load setup health. Showing default checklist — retry to refresh live data."
          onRetry={loadHealth}
        />
      )}

      <div className="bg-fd-panel border border-slate-700 rounded-lg p-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="text-sm text-slate-400 uppercase tracking-wide">Onboarding Progress</div>
            <div className="text-3xl font-semibold text-white mt-1">{health?.progress ?? 0}%</div>
          </div>
          <Link to={currentStep.href} className="btn-primary flex items-center">
            <PlayCircleIcon className="h-5 w-5 mr-2" />
            Continue Setup
          </Link>
        </div>
        <div className="mt-4 h-3 rounded-full bg-slate-800 overflow-hidden">
          <div className="h-full rounded-full bg-cyan-500 transition-all" style={{ width: `${health?.progress ?? 0}%` }} />
        </div>
      </div>

      <div className="bg-fd-panel border border-slate-700 rounded-lg p-5">
        <div className="flex items-center gap-2 mb-4">
          <SparklesIcon className="h-5 w-5 text-cyan-300" />
          <h2 className="text-lg font-semibold text-white">Review Queue</h2>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
          {reviewQueue.map((item) => (
            <Link
              key={item.key}
              to={item.href}
              className="group rounded-lg border border-slate-700 bg-slate-900/40 p-4 hover:border-cyan-500/60 transition-colors"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-semibold text-white">{item.title}</div>
                  <div className="text-xs text-slate-400 mt-2 leading-5">{item.detail}</div>
                </div>
                <span className={`shrink-0 rounded px-2 py-1 text-xs font-semibold ${
                  item.count > 0 ? 'bg-amber-500/20 text-amber-300' : 'bg-emerald-500/20 text-emerald-300'
                }`}>
                  {item.count}
                </span>
              </div>
              <div className="mt-3 flex items-center text-xs font-medium text-cyan-300">
                Open <ArrowRightIcon className="h-3.5 w-3.5 ml-1 group-hover:translate-x-0.5 transition-transform" />
              </div>
            </Link>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {steps.map((step, index) => (
          <Link
            key={step.key}
            to={step.href}
            className="bg-fd-panel border border-slate-700 rounded-lg p-4 hover:border-cyan-500/60 transition-colors"
          >
            <div className="flex items-start gap-3">
              <div className={`h-9 w-9 rounded-lg flex items-center justify-center ${
                step.status === 'complete' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-amber-500/20 text-amber-300'
              }`}>
                {step.status === 'complete' ? <CheckCircleIcon className="h-5 w-5" /> : <span className="font-semibold">{index + 1}</span>}
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-white">{step.label}</div>
                <div className="text-sm text-slate-400 mt-1">
                  {step.status === 'complete' ? `${step.count} found` : step.reason}
                </div>
              </div>
            </div>
          </Link>
        ))}
      </div>

      <div className="bg-fd-panel border border-slate-700 rounded-lg p-5">
        <div className="flex items-center gap-2 mb-4">
          <ExclamationTriangleIcon className="h-5 w-5 text-amber-300" />
          <h2 className="text-lg font-semibold text-white">Master Data Health</h2>
        </div>
        {health?.issues?.length ? (
          <div className="space-y-3">
            {health.issues.map((issue) => (
              <Link key={issue.key} to={issue.href} className="block rounded-lg border border-slate-700 bg-slate-900/40 p-3 hover:border-amber-500/60">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-medium text-white">{issue.title}</div>
                    <div className="text-sm text-slate-400 mt-1">{issue.detail}</div>
                  </div>
                  <span className={`px-2 py-1 rounded text-xs font-semibold ${
                    issue.severity === 'high' ? 'bg-red-500/20 text-red-300' : 'bg-amber-500/20 text-amber-300'
                  }`}>
                    {issue.count}
                  </span>
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={CheckCircleIcon}
            title="No blocking issues"
            description="No blocking master-data issues found. Your data is ready for production."
          />
        )}
      </div>
    </div>
  );
}
