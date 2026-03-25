import React, { useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTour } from '../../context/TourContext';
import { getToursForRole, getHelpTipsForRole, getTour } from '../../data/tours';
import { useAuth } from '../../context/AuthContext';
import { usePermissions } from '../../hooks/usePermissions';
import { ROLE_LABELS } from '../../utils/permissions';
import {
  QuestionMarkCircleIcon,
  PlayIcon,
  CheckCircleIcon,
  ArrowPathIcon,
  XMarkIcon,
  LightBulbIcon,
  RocketLaunchIcon,
  ClipboardDocumentListIcon,
  WrenchScrewdriverIcon,
  CogIcon,
  ShieldCheckIcon,
  CalculatorIcon,
  TruckIcon,
  Cog6ToothIcon,
  UserCircleIcon,
  CommandLineIcon,
} from '@heroicons/react/24/outline';

// Map icon name strings from tour data to actual icon components
const ICON_MAP: Record<string, React.ComponentType<React.SVGProps<SVGSVGElement>>> = {
  RocketLaunchIcon,
  ClipboardDocumentListIcon,
  WrenchScrewdriverIcon,
  CogIcon,
  ShieldCheckIcon,
  CalculatorIcon,
  TruckIcon,
  Cog6ToothIcon,
};

const CATEGORY_LABELS: Record<string, string> = {
  'getting-started': 'Getting Started',
  production: 'Production',
  engineering: 'Engineering',
  quality: 'Quality',
  admin: 'Administration',
};

const CATEGORY_ORDER = ['getting-started', 'production', 'engineering', 'quality', 'admin'];

type Tab = 'tours' | 'tips';

export default function TourMenu() {
  const [isOpen, setIsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>('tours');
  const { startTour, isTourComplete, resetAllTours, isActive } = useTour();
  const { user } = useAuth();
  const { role, isSuperuser } = usePermissions();
  const navigate = useNavigate();

  // Get role-filtered tours and tips
  const roleTours = useMemo(
    () => getToursForRole(role, isSuperuser),
    [role, isSuperuser]
  );

  const helpTips = useMemo(
    () => getHelpTipsForRole(role, isSuperuser),
    [role, isSuperuser]
  );

  // Group tours by category
  const toursByCategory = useMemo(() => {
    const groups: Record<string, typeof roleTours> = {};
    for (const tour of roleTours) {
      const cat = tour.category || 'getting-started';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(tour);
    }
    return groups;
  }, [roleTours]);

  // Completion stats
  const completedCount = roleTours.filter((t) => isTourComplete(t.id)).length;
  const totalCount = roleTours.length;
  const progressPct = totalCount > 0 ? Math.round((completedCount / totalCount) * 100) : 0;

  const handleStartTour = (tourId: string) => {
    // Use the role-filtered tour (with customized descriptions/steps)
    const filteredTour = roleTours.find((t) => t.id === tourId);
    const tour = filteredTour || getTour(tourId);
    if (tour) {
      setIsOpen(false);
      if (tour.startPath) {
        navigate(tour.startPath);
        setTimeout(() => {
          startTour(tour);
        }, 600);
      } else {
        startTour(tour);
      }
    }
  };

  if (isActive) return null;

  const roleLabel = role ? ROLE_LABELS[role] : 'User';

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="p-2 rounded-xl text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-colors"
        aria-label="Help & Tours"
        data-tour="help-menu"
      >
        <QuestionMarkCircleIcon className="h-5 w-5" />
      </button>

      {isOpen && (
        <>
          {/* Backdrop */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => setIsOpen(false)}
          />

          {/* Menu */}
          <div className="absolute right-0 top-full mt-2 w-96 bg-white rounded-2xl shadow-2xl border border-slate-200 overflow-hidden z-50 animate-slide-down">
            {/* Header with role badge */}
            <div className="bg-gradient-to-r from-werco-navy-600 to-blue-700 px-5 py-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <QuestionMarkCircleIcon className="h-5 w-5 text-white" />
                  <h3 className="text-white font-bold">Help & Tours</h3>
                </div>
                <button
                  onClick={() => setIsOpen(false)}
                  className="text-white/70 hover:text-white p-1 rounded-lg hover:bg-white/10"
                >
                  <XMarkIcon className="h-5 w-5" />
                </button>
              </div>

              {/* Role info bar */}
              <div className="flex items-center gap-2 bg-white/10 rounded-xl px-3 py-2">
                <UserCircleIcon className="h-4 w-4 text-white/80 flex-shrink-0" />
                <span className="text-white/90 text-sm">
                  Showing help for{' '}
                  <span className="font-semibold text-white">{roleLabel}</span>
                  {user?.first_name && (
                    <span className="text-white/70"> — {user.first_name}</span>
                  )}
                </span>
              </div>

              {/* Progress bar */}
              {totalCount > 0 && (
                <div className="mt-3">
                  <div className="flex items-center justify-between text-xs text-white/80 mb-1">
                    <span>Tour Progress</span>
                    <span>
                      {completedCount}/{totalCount} completed
                    </span>
                  </div>
                  <div className="h-1.5 bg-white/20 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-emerald-400 rounded-full transition-all duration-500"
                      style={{ width: `${progressPct}%` }}
                    />
                  </div>
                </div>
              )}
            </div>

            {/* Tabs */}
            <div className="flex border-b border-slate-100">
              <button
                onClick={() => setActiveTab('tours')}
                className={`flex-1 flex items-center justify-center gap-1.5 px-4 py-2.5 text-sm font-medium transition-colors ${
                  activeTab === 'tours'
                    ? 'text-werco-navy-600 border-b-2 border-werco-navy-600'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                <RocketLaunchIcon className="h-4 w-4" />
                Guided Tours
                {totalCount > 0 && (
                  <span className="text-xs bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded-full">
                    {totalCount}
                  </span>
                )}
              </button>
              <button
                onClick={() => setActiveTab('tips')}
                className={`flex-1 flex items-center justify-center gap-1.5 px-4 py-2.5 text-sm font-medium transition-colors ${
                  activeTab === 'tips'
                    ? 'text-werco-navy-600 border-b-2 border-werco-navy-600'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                <LightBulbIcon className="h-4 w-4" />
                Quick Tips
                {helpTips.length > 0 && (
                  <span className="text-xs bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded-full">
                    {helpTips.length}
                  </span>
                )}
              </button>
            </div>

            {/* Content area */}
            <div className="max-h-80 overflow-y-auto">
              {activeTab === 'tours' && (
                <div className="p-3">
                  {CATEGORY_ORDER.filter((cat) => toursByCategory[cat]).map(
                    (category) => (
                      <div key={category} className="mb-3 last:mb-0">
                        <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider px-3 mb-1">
                          {CATEGORY_LABELS[category]}
                        </h4>
                        {toursByCategory[category].map((tour) => {
                          const isComplete = isTourComplete(tour.id);
                          const IconComponent =
                            tour.icon && ICON_MAP[tour.icon]
                              ? ICON_MAP[tour.icon]
                              : RocketLaunchIcon;
                          return (
                            <button
                              key={tour.id}
                              onClick={() => handleStartTour(tour.id)}
                              className="w-full flex items-start gap-3 p-3 rounded-xl hover:bg-slate-50 transition-colors text-left group"
                            >
                              <div
                                className={`flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center ${
                                  isComplete
                                    ? 'bg-emerald-100 text-emerald-600'
                                    : 'bg-blue-50 text-werco-navy-600 group-hover:bg-blue-100'
                                }`}
                              >
                                {isComplete ? (
                                  <CheckCircleIcon className="h-4.5 w-4.5" />
                                ) : (
                                  <IconComponent className="h-4.5 w-4.5" />
                                )}
                              </div>
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2">
                                  <span className="font-medium text-sm text-slate-800">
                                    {tour.name}
                                  </span>
                                  {isComplete && (
                                    <CheckCircleIcon className="h-3.5 w-3.5 text-emerald-500 flex-shrink-0" />
                                  )}
                                </div>
                                <p className="text-xs text-slate-500 mt-0.5 line-clamp-2">
                                  {tour.description}
                                </p>
                                <div className="flex items-center gap-2 mt-1">
                                  <span className="text-xs text-slate-400">
                                    {tour.steps.length} steps
                                  </span>
                                  {!isComplete && (
                                    <span className="flex items-center gap-0.5 text-xs text-blue-600 opacity-0 group-hover:opacity-100 transition-opacity">
                                      <PlayIcon className="h-3 w-3" />
                                      Start
                                    </span>
                                  )}
                                  {isComplete && (
                                    <span className="flex items-center gap-0.5 text-xs text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity">
                                      <ArrowPathIcon className="h-3 w-3" />
                                      Replay
                                    </span>
                                  )}
                                </div>
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    )
                  )}
                  {roleTours.length === 0 && (
                    <div className="text-center py-6 text-slate-400 text-sm">
                      No tours available for your role.
                    </div>
                  )}
                </div>
              )}

              {activeTab === 'tips' && (
                <div className="p-3 space-y-1">
                  {helpTips.map((tip) => (
                    <div
                      key={tip.id}
                      className="flex items-start gap-3 p-3 rounded-xl hover:bg-amber-50/50 transition-colors"
                    >
                      <div className="flex-shrink-0 w-8 h-8 rounded-lg bg-amber-100 text-amber-600 flex items-center justify-center">
                        <LightBulbIcon className="h-4 w-4" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-sm text-slate-800">
                            {tip.title}
                          </span>
                          {tip.shortcut && (
                            <kbd className="text-xs bg-slate-100 text-slate-500 px-1.5 py-0.5 rounded border border-slate-200 font-mono">
                              {tip.shortcut}
                            </kbd>
                          )}
                        </div>
                        <p className="text-xs text-slate-500 mt-0.5">
                          {tip.description}
                        </p>
                      </div>
                    </div>
                  ))}
                  {helpTips.length === 0 && (
                    <div className="text-center py-6 text-slate-400 text-sm">
                      No tips available for your role.
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="border-t border-slate-100 p-3 flex items-center gap-2">
              <button
                onClick={() => {
                  resetAllTours();
                }}
                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-slate-500 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors"
              >
                <ArrowPathIcon className="h-3.5 w-3.5" />
                Reset Tours
              </button>
              <div className="w-px h-4 bg-slate-200" />
              <button
                onClick={() => {
                  setIsOpen(false);
                  navigate('/');
                }}
                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-slate-500 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors"
              >
                <CommandLineIcon className="h-3.5 w-3.5" />
                Keyboard Shortcuts
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
