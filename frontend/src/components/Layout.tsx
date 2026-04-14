import React, { useState, useEffect, useMemo } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { TourMenu } from './Tour';
import SessionWarningModal from './SessionWarningModal';
import GlobalSearch, { useGlobalSearch } from './GlobalSearch';
import BottomNav from './ui/BottomNav';
import SkipLink from './SkipLink';
import { useKeyboardShortcuts, GLOBAL_SHORTCUTS } from '../hooks/useKeyboardShortcuts';
import { useKeyboardShortcutsContext } from '../context/KeyboardShortcutsContext';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { isKioskMode } from '../utils/kiosk';
import {
  HomeIcon,
  ClipboardDocumentListIcon,
  CogIcon,
  CubeIcon,
  WrenchScrewdriverIcon,
  ArrowRightOnRectangleIcon,
  Bars3Icon,
  XMarkIcon,
  DocumentDuplicateIcon,
  CalculatorIcon,
  AdjustmentsHorizontalIcon,
  ListBulletIcon,
  ShieldCheckIcon,
  ArchiveBoxIcon,
  TruckIcon,
  InboxArrowDownIcon,
  CalendarDaysIcon,
  DocumentTextIcon,
  ChartBarIcon,
  PaperAirplaneIcon,
  CurrencyDollarIcon,
  SparklesIcon,
  UsersIcon,
  BuildingOfficeIcon,
  WrenchScrewdriverIcon as WrenchIcon2,
  DocumentMagnifyingGlassIcon,
  ChevronDownIcon,
  Cog6ToothIcon,
  MagnifyingGlassIcon,
  StopIcon,
} from '@heroicons/react/24/outline';

interface LayoutProps {
  children: React.ReactNode;
}

interface NavItem {
  name: string;
  href?: string;
  icon: React.ComponentType<{ className?: string }>;
  children?: NavItem[];
  badge?: number;
  adminOnly?: boolean;
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/', icon: HomeIcon },
  {
    name: 'Shop Floor',
    icon: WrenchScrewdriverIcon,
    children: [
      { name: 'Time Clock', href: '/shop-floor', icon: WrenchScrewdriverIcon },
      { name: 'Operations', href: '/shop-floor/operations', icon: ClipboardDocumentListIcon },
      { name: 'Downtime', href: '/downtime', icon: StopIcon },
    ],
  },
  { name: 'Scheduling', href: '/scheduling', icon: CalendarDaysIcon },
  { name: 'Work Orders', href: '/work-orders', icon: ClipboardDocumentListIcon },
  {
    name: 'Engineering',
    icon: CubeIcon,
    children: [
      { name: 'Parts', href: '/parts', icon: CubeIcon },
      { name: 'Bill of Materials', href: '/bom', icon: DocumentDuplicateIcon },
      { name: 'Routing', href: '/routing', icon: ListBulletIcon },
      { name: 'Engineering Changes', href: '/engineering-changes', icon: DocumentDuplicateIcon },
    ],
  },
  {
    name: 'Warehouse',
    icon: ArchiveBoxIcon,
    children: [
      { name: 'Inventory', href: '/warehouse?tab=inventory', icon: ArchiveBoxIcon },
      { name: 'Receiving', href: '/warehouse?tab=receiving', icon: InboxArrowDownIcon },
      { name: 'Shipping', href: '/warehouse?tab=shipping', icon: PaperAirplaneIcon },
    ],
  },
  {
    name: 'Purchasing',
    icon: TruckIcon,
    children: [
      { name: 'Purchase Orders', href: '/purchasing', icon: TruckIcon },
      { name: 'Upload PO', href: '/po-upload', icon: DocumentDuplicateIcon },
      { name: 'MRP', href: '/mrp', icon: CalculatorIcon },
    ],
  },
  {
    name: 'Sales & Quoting',
    icon: CurrencyDollarIcon,
    children: [
      { name: 'AI RFQ Quote', href: '/rfq-packages/new', icon: SparklesIcon },
      { name: 'Quote Calculator', href: '/quote-calculator', icon: CalculatorIcon },
      { name: 'Quotes', href: '/quotes', icon: CurrencyDollarIcon },
      { name: 'Customers', href: '/customers', icon: BuildingOfficeIcon },
    ],
  },
  {
    name: 'Quality',
    icon: ShieldCheckIcon,
    children: [
      { name: 'NCR / CAR / FAI', href: '/quality', icon: ShieldCheckIcon },
      { name: 'SPC', href: '/spc', icon: ChartBarIcon },
      { name: 'Calibration', href: '/calibration', icon: WrenchIcon2 },
      { name: 'Traceability', href: '/traceability', icon: DocumentMagnifyingGlassIcon },
      { name: 'Customer Complaints', href: '/customer-complaints', icon: DocumentTextIcon },
      { name: 'QMS Standards', href: '/qms-standards', icon: DocumentMagnifyingGlassIcon },
    ],
  },
  { name: 'Maintenance', href: '/maintenance', icon: WrenchIcon2 },
  { name: 'Tool Management', href: '/tool-management', icon: WrenchScrewdriverIcon },
  { name: 'OEE', href: '/oee', icon: ChartBarIcon },
  { name: 'Documents', href: '/documents', icon: DocumentTextIcon },
  { name: 'Job Costing', href: '/job-costing', icon: CurrencyDollarIcon },
  { name: 'Analytics', href: '/analytics', icon: ChartBarIcon },
  { name: 'Reports', href: '/reports', icon: DocumentTextIcon },
  {
    name: 'Administration',
    icon: Cog6ToothIcon,
    children: [
      { name: 'Work Centers', href: '/work-centers', icon: CogIcon },
      { name: 'Users', href: '/users', icon: UsersIcon },
      { name: 'Operator Certifications', href: '/certifications', icon: ShieldCheckIcon },
      { name: 'Supplier Scorecards', href: '/supplier-scorecards', icon: ChartBarIcon },
      { name: 'Custom Fields', href: '/custom-fields', icon: AdjustmentsHorizontalIcon },
      { name: 'Admin Settings', href: '/admin/settings', icon: Cog6ToothIcon, adminOnly: true },
      { name: 'Audit Log', href: '/audit-log', icon: ShieldCheckIcon },
    ],
  },
];

/** Check if a nav href matches the current location (supports query params in href) */
function isHrefActive(href: string | undefined, location: { pathname: string; search: string }): boolean {
  if (!href) return false;
  if (href.includes('?')) {
    const [path, query] = href.split('?');
    if (location.pathname !== path) return false;
    const hrefParams = new URLSearchParams(query);
    const locParams = new URLSearchParams(location.search);
    let match = true;
    hrefParams.forEach((value, key) => {
      if (locParams.get(key) !== value) match = false;
    });
    if (!match) return false;
    return true;
  }
  return location.pathname === href;
}

const NavGroup = React.memo(function NavGroup({
  item,
  location,
  onNavigate,
  collapsed,
  isAdmin,
}: {
  item: NavItem;
  location: any;
  onNavigate?: () => void;
  collapsed?: boolean;
  isAdmin?: boolean;
}) {
  // Filter children based on admin status
  const visibleChildren = useMemo(
    () => item.children?.filter(child => !child.adminOnly || isAdmin),
    [item.children, isAdmin]
  );

  const [isOpen, setIsOpen] = useState(() => {
    if (visibleChildren) {
      return visibleChildren.some(child => isHrefActive(child.href, location));
    }
    return false;
  });

  // Auto-open when navigating to a child
  useEffect(() => {
    if (visibleChildren?.some(child => isHrefActive(child.href, location))) {
      setIsOpen(true);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname, location.search, visibleChildren]);

  const isActive = isHrefActive(item.href, location);
  const hasActiveChild = visibleChildren?.some(child => isHrefActive(child.href, location));

  if (item.href) {
    return (
      <Link
        to={item.href}
        className={`
          group flex items-center gap-3 px-3 py-2.5 rounded-xl
          text-sm font-medium transition-all duration-200
          ${isActive ? 'bg-white/15 text-white shadow-sm' : 'text-white/70 hover:bg-white/10 hover:text-white'}
        `}
        onClick={onNavigate}
        title={collapsed ? item.name : undefined}
      >
        <item.icon
          className={`h-5 w-5 flex-shrink-0 transition-colors ${isActive ? 'text-white' : 'text-white/60 group-hover:text-white'}`}
        />
        {!collapsed && <span>{item.name}</span>}
        {item.badge && !collapsed && (
          <span className="ml-auto bg-accent-500 text-white text-xs font-bold px-2 py-0.5 rounded-full">
            {item.badge}
          </span>
        )}
      </Link>
    );
  }

  return (
    <div>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          group w-full flex items-center gap-3 px-3 py-2.5 rounded-xl
          text-sm font-medium transition-all duration-200
          ${hasActiveChild ? 'bg-white/10 text-white' : 'text-white/70 hover:bg-white/10 hover:text-white'}
        `}
        title={collapsed ? item.name : undefined}
      >
        <item.icon
          className={`h-5 w-5 flex-shrink-0 transition-colors ${hasActiveChild ? 'text-white' : 'text-white/60 group-hover:text-white'}`}
        />
        {!collapsed && (
          <>
            <span className="flex-1 text-left">{item.name}</span>
            <ChevronDownIcon className={`h-4 w-4 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
          </>
        )}
      </button>

      {!collapsed && isOpen && visibleChildren && visibleChildren.length > 0 && (
        <div className="mt-1 ml-3 pl-3 border-l-2 border-white/20 space-y-0.5">
          {visibleChildren.map(child => {
            const isChildActive = isHrefActive(child.href, location);
            return (
              <Link
                key={child.name}
                to={child.href!}
                className={`
                  flex items-center gap-2.5 px-3 py-2 rounded-lg
                  text-sm transition-all duration-200
                  ${
                    isChildActive
                      ? 'bg-white/15 text-white font-medium'
                      : 'text-white/60 hover:bg-white/10 hover:text-white'
                  }
                `}
                onClick={onNavigate}
              >
                <child.icon className="h-4 w-4 flex-shrink-0" />
                <span>{child.name}</span>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
});

// Blueprint grid pattern for sidebar - matches wercomfg.com aesthetic
const SidebarPattern = () => (
  <svg className="absolute inset-0 w-full h-full opacity-[0.03]" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <pattern id="sidebar-blueprint" width="40" height="40" patternUnits="userSpaceOnUse">
        <path
          d="M0 0h40v40H0z"
          fill="none"
          stroke="currentColor"
          strokeWidth="0.5"
          className="text-blue-300"
        />
        <path
          d="M20 0v40M0 20h40"
          fill="none"
          stroke="currentColor"
          strokeWidth="0.25"
          className="text-blue-300"
        />
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#sidebar-blueprint)" />
  </svg>
);

export default function Layout({ children }: LayoutProps) {
  const { user, logout, logoutWithEmployeeId } = useAuth();
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [logoutModalOpen, setLogoutModalOpen] = useState(false);
  const [logoutEmployeeId, setLogoutEmployeeId] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const globalSearch = useGlobalSearch();
  const keyboardShortcuts = useKeyboardShortcutsContext();
  const isKiosk = isKioskMode(location.pathname, location.search) && user?.role === 'operator';
  const presenceUrl = useMemo(() => {
    if (!user) return null;
    const token = getAccessToken();
    if (!token) return null;
    return buildWsUrl('/ws/updates', { token });
  }, [user]);

  useWebSocket({
    url: presenceUrl,
    enabled: Boolean(presenceUrl),
  });

  // Global keyboard shortcuts for layout
  useKeyboardShortcuts([
    {
      ...GLOBAL_SHORTCUTS.SEARCH,
      action: globalSearch.open,
      preventDefault: true,
    },
  ]);

  // Close sidebar on route change (mobile)
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  // Get current page title
  const pageTitle = useMemo(() => {
    for (const item of navigation) {
      if (isHrefActive(item.href, location)) return item.name;
      if (item.children) {
        const child = item.children.find(c => isHrefActive(c.href, location));
        if (child) return child.name;
      }
    }
    return 'Werco ERP';
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname, location.search]);

  const isShopFloorKiosk = useMemo(
    () =>
      location.pathname.startsWith('/shop-floor') &&
      isKioskMode(location.pathname, location.search) &&
      user?.role === 'operator',
    [location.pathname, location.search, user?.role]
  );

  const isOperator = user?.role === 'operator';

  const visibleNavigation = useMemo(() => {
    if (isKiosk) {
      return navigation.filter(item => item.name === 'Shop Floor');
    }

    // Operators see a streamlined nav: Shop Floor, Quality, and Downtime
    if (isOperator) {
      const operatorAllowed = new Set(['Dashboard', 'Shop Floor', 'Quality', 'Maintenance']);
      return navigation.filter(item => operatorAllowed.has(item.name));
    }

    return navigation;
  }, [isKiosk, isOperator]);

  const handleLogoutConfirm = async () => {
    setLogoutError('');
    try {
      await logoutWithEmployeeId(logoutEmployeeId);
      setLogoutModalOpen(false);
      setLogoutEmployeeId('');
    } catch (err: any) {
      setLogoutError(err?.message || 'Employee ID did not match. Please try again.');
    }
  };

  return (
    <div className="min-h-screen" style={{ background: '#0d1117' }}>
      {/* Skip to main content link for accessibility */}
      <SkipLink />

      {/* Mobile sidebar backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/60 backdrop-blur-sm lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar - Werco Navy */}
      <aside
        className={`
          fixed inset-y-0 left-0 z-50 w-72
          transform transition-transform duration-300 ease-out
          lg:translate-x-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          flex flex-col overflow-hidden
        `}
        style={{
          background: 'linear-gradient(180deg, #0a1628 0%, #0f2952 40%, #0a1628 100%)',
        }}
      >
        {/* Blueprint grid background */}
        <SidebarPattern />
        <div className="absolute top-1/4 -left-20 w-40 h-40 bg-blue-500/8 rounded-full blur-3xl" />
        <div className="absolute bottom-1/4 -right-20 w-60 h-60 bg-blue-600/6 rounded-full blur-3xl" />

        {/* Logo header */}
        <div className="relative flex items-center justify-between h-20 px-4 border-b border-white/10 flex-shrink-0">
          <Link to="/" className="flex items-center gap-3">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-12 w-auto brightness-0 invert" />
          </Link>
          <button
            onClick={() => setSidebarOpen(false)}
            className="lg:hidden p-2 rounded-lg text-white/60 hover:text-white hover:bg-white/10 transition-colors"
            aria-label="Close navigation menu"
          >
            <XMarkIcon className="h-6 w-6" aria-hidden="true" />
          </button>
        </div>

        {/* Navigation */}
        <nav
          className="relative flex-1 px-3 py-4 space-y-1 overflow-y-auto scrollbar-hide"
          data-tour="sidebar"
          aria-label="Main navigation"
        >
          {visibleNavigation.map(item => (
            <NavGroup
              key={item.name}
              item={item}
              location={location}
              onNavigate={() => setSidebarOpen(false)}
              isAdmin={user?.role === 'admin'}
            />
          ))}
        </nav>

        {/* Certifications strip */}
        <div className="relative flex-shrink-0 px-4 py-2 border-t border-white/5">
          <div className="flex items-center justify-center gap-3 text-[10px] font-mono uppercase tracking-widest text-white/30">
            <span>AS9100D</span>
            <span className="text-white/15">|</span>
            <span>ISO 9001</span>
            <span className="text-white/15">|</span>
            <span>ITAR</span>
          </div>
        </div>

        {/* User section */}
        <div className="relative flex-shrink-0 p-4 border-t border-white/10 bg-white/[0.03]">
          <div className="flex items-center gap-3">
            <div className="flex-shrink-0">
              <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-werco-navy-600 to-blue-700 flex items-center justify-center text-white font-semibold text-sm shadow-lg ring-1 ring-white/10">
                {user?.first_name?.[0]}
                {user?.last_name?.[0]}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white truncate">
                {user?.first_name} {user?.last_name}
              </p>
              <p className="text-xs text-blue-300/60 truncate capitalize">{user?.role?.replace('_', ' ')}</p>
            </div>
            <button
              onClick={() => {
                if (isShopFloorKiosk) {
                  setLogoutModalOpen(true);
                } else {
                  logout();
                }
              }}
              className="p-2 rounded-lg text-white/60 hover:text-white hover:bg-white/10 transition-all duration-200"
              title="Sign out"
            >
              <ArrowRightOnRectangleIcon className="h-5 w-5" />
            </button>
          </div>
        </div>
      </aside>

      {/* Main content area */}
      <div className="lg:pl-72 flex flex-col min-h-screen">
        {/* Top bar - Clean white with subtle border */}
        <header className="sticky top-0 z-30 bg-[#151b28]/90 backdrop-blur-xl border-b border-slate-700/50 shadow-sm">
          <div className="flex items-center justify-between h-16 px-4 sm:px-6 lg:px-8">
            {/* Mobile menu button */}
            <button
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden p-2 -ml-2 rounded-xl text-slate-400 hover:bg-slate-700 transition-colors"
            >
              <Bars3Icon className="h-6 w-6" />
            </button>

            {/* Page title (desktop) */}
            <div className="hidden lg:block">
              <h1 className="text-lg font-semibold text-slate-100">{pageTitle}</h1>
            </div>

            {/* Mobile logo */}
            <div className="lg:hidden">
              <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-10 w-auto brightness-0 invert" />
            </div>

            {/* Right side actions */}
            <div className="flex items-center gap-2" data-tour="user-menu">
              {/* Quick search button */}
              <button
                onClick={globalSearch.open}
                className="flex items-center gap-2 px-3 py-2 rounded-xl text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition-all duration-200 border border-slate-600"
                title="Search (Ctrl+K)"
                data-tour="search"
              >
                <MagnifyingGlassIcon className="h-4 w-4" />
                <span className="hidden md:inline text-sm">Search...</span>
                <kbd className="hidden md:inline-flex items-center px-1.5 py-0.5 text-xs font-medium text-slate-500 bg-slate-700 rounded">
                  Ctrl+K
                </kbd>
              </button>

              {/* Keyboard shortcuts help */}
              <button
                onClick={keyboardShortcuts.showHelp}
                className="hidden md:flex items-center justify-center p-2 rounded-xl text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition-all duration-200"
                title="Keyboard shortcuts (Ctrl+/)"
                aria-label="Show keyboard shortcuts"
              >
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z"
                  />
                </svg>
              </button>

              {/* Help & Tours menu */}
              <div className="hidden lg:block">
                <TourMenu />
              </div>

              {/* User avatar (mobile) */}
              <div className="lg:hidden flex items-center">
                <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-werco-navy-600 to-blue-700 flex items-center justify-center text-white font-medium text-sm shadow-md">
                  {user?.first_name?.[0]}
                  {user?.last_name?.[0]}
                </div>
              </div>
            </div>
          </div>
        </header>

        {/* Main content - extra bottom padding for mobile nav */}
        <main id="main-content" className="flex-1 p-4 sm:p-6 lg:p-8 pb-20 lg:pb-8" role="main" tabIndex={-1}>
          <div className="max-w-7xl mx-auto animate-fade-in">{children}</div>
        </main>

        {/* Footer - Hidden on mobile, visible on desktop */}
        <footer className="hidden lg:block flex-shrink-0 py-4 px-6 border-t border-slate-700/50 bg-[#0f1419]/60">
          <div className="max-w-7xl mx-auto flex items-center justify-between text-sm text-slate-500">
            <div className="flex items-center gap-2">
              <span className="font-medium text-slate-300">Werco Manufacturing</span>
              <span className="text-slate-600">|</span>
              <span className="text-blue-400 font-semibold">ERP / MES</span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500">AS9100D &middot; ISO 9001 &middot; ITAR</span>
              <span className="text-slate-600">|</span>
              <span className="text-slate-500">v1.0.0</span>
            </div>
          </div>
        </footer>
      </div>

      {/* Bottom Navigation - Mobile only */}
      {!isKiosk && <BottomNav onMenuClick={() => setSidebarOpen(true)} />}

      {/* Global Search Modal */}
      <GlobalSearch isOpen={globalSearch.isOpen} onClose={globalSearch.close} />

      {/* Session Warning Modal */}
      <SessionWarningModal />

      {/* Employee ID Logout Modal */}
      {logoutModalOpen && isShopFloorKiosk && (
        <div className="du-modal du-modal-open" onClick={() => setLogoutModalOpen(false)}>
          <div className="du-modal-box max-w-sm p-0 overflow-hidden" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-base-300">
              <h3 className="text-lg font-semibold text-base-content">Sign out</h3>
              <button
                onClick={() => setLogoutModalOpen(false)}
                className="du-btn du-btn-sm du-btn-circle du-btn-ghost"
                aria-label="Close"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="px-6 py-4 space-y-3">
              <p className="text-sm text-base-content/70">Enter your employee ID (or 4-digit badge ID) to sign out.</p>
              <input
                type="text"
                inputMode="text"
                maxLength={50}
                value={logoutEmployeeId}
                onChange={e => setLogoutEmployeeId(e.target.value.replace(/[^A-Za-z0-9\-_]/g, '').slice(0, 50))}
                className="du-input du-input-bordered w-full text-center text-lg"
                placeholder="0000 or EMP-1001"
                autoFocus
              />
              {logoutError && <div className="du-alert du-alert-error py-2 text-sm">{logoutError}</div>}
            </div>

            <div className="du-modal-action mt-0 px-6 py-4 bg-base-200/60 border-t border-base-300 justify-end gap-3">
              <button
                onClick={() => {
                  setLogoutModalOpen(false);
                  setLogoutEmployeeId('');
                  setLogoutError('');
                }}
                className="du-btn du-btn-ghost"
              >
                Cancel
              </button>
              <button
                onClick={handleLogoutConfirm}
                className="du-btn du-btn-error"
                disabled={logoutEmployeeId.trim().length === 0}
              >
                Sign Out
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
