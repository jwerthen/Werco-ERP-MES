import React, { useState, useEffect, useMemo } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import CompanySwitcher from './CompanySwitcher';
import ReadOnlyBanner from './ReadOnlyBanner';
import { TourMenu } from './Tour';
import SessionWarningModal from './SessionWarningModal';
import GlobalSearch, { useGlobalSearch } from './GlobalSearch';
import BottomNav from './ui/BottomNav';
import SkipLink from './SkipLink';
import AdaptivePromptPanel from './AdaptivePromptPanel';
import api from '../services/api';
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
  ArrowUpTrayIcon,
  RocketLaunchIcon,
  BellAlertIcon,
  UserPlusIcon,
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
  platformOnly?: boolean;
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/', icon: HomeIcon },
  { name: 'Action Inbox', href: '/action-inbox', icon: BellAlertIcon },
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
      { name: 'Materials & Supplies', href: '/materials', icon: WrenchScrewdriverIcon },
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
      { name: 'Setup Wizard', href: '/setup', icon: RocketLaunchIcon },
      { name: 'Import Center', href: '/import-center', icon: ArrowUpTrayIcon },
      { name: 'Work Centers', href: '/work-centers', icon: CogIcon },
      { name: 'Users', href: '/users', icon: UsersIcon },
      { name: 'User Approvals', href: '/users?approvals=pending', icon: UserPlusIcon, adminOnly: true },
      { name: 'Operator Certifications', href: '/certifications', icon: ShieldCheckIcon },
      { name: 'Supplier Scorecards', href: '/supplier-scorecards', icon: ChartBarIcon },
      { name: 'Custom Fields', href: '/custom-fields', icon: AdjustmentsHorizontalIcon },
      { name: 'Admin Settings', href: '/admin/settings', icon: Cog6ToothIcon, adminOnly: true },
      { name: 'Platform Overview', href: '/platform', icon: BuildingOfficeIcon, platformOnly: true },
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
  isPlatformAdmin,
}: {
  item: NavItem;
  location: any;
  onNavigate?: () => void;
  collapsed?: boolean;
  isAdmin?: boolean;
  isPlatformAdmin?: boolean;
}) {
  // Filter children based on admin status
  const visibleChildren = useMemo(
    () => item.children?.filter(child =>
      (!child.adminOnly || isAdmin) &&
      (!child.platformOnly || isPlatformAdmin)
    ),
    [item.children, isAdmin, isPlatformAdmin]
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

  }, [location.pathname, location.search, visibleChildren]);

  const isActive = isHrefActive(item.href, location);
  const hasActiveChild = visibleChildren?.some(child => isHrefActive(child.href, location));

  if (item.href) {
    return (
      <Link
        to={item.href}
        className={`
          group relative flex items-center gap-3 px-3 py-2 rounded-[3px]
          text-[13px] font-medium transition-all duration-150
          ${
            isActive
              ? 'bg-[rgba(47,129,247,0.1)] text-fd-ink shadow-[inset_2px_0_0_#2f81f7]'
              : 'text-fd-body hover:bg-white/[0.03] hover:text-fd-ink'
          }
        `}
        onClick={onNavigate}
        title={collapsed ? item.name : undefined}
      >
        <item.icon
          className={`h-[17px] w-[17px] flex-shrink-0 transition-colors ${isActive ? 'text-fd-blue' : 'text-fd-mute group-hover:text-fd-body'}`}
        />
        {!collapsed && <span className="flex-1">{item.name}</span>}
        {item.badge && !collapsed && (
          <span className="ml-auto bg-fd-red text-white text-[10px] font-bold font-mono px-1.5 py-0.5 rounded-[3px]">
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
          group w-full flex items-center gap-3 px-3 py-2 rounded-[3px]
          text-[13px] font-medium transition-all duration-150
          ${hasActiveChild ? 'text-fd-ink' : 'text-fd-body hover:bg-white/[0.03] hover:text-fd-ink'}
        `}
        title={collapsed ? item.name : undefined}
      >
        <item.icon
          className={`h-[17px] w-[17px] flex-shrink-0 transition-colors ${hasActiveChild ? 'text-fd-blue' : 'text-fd-mute group-hover:text-fd-body'}`}
        />
        {!collapsed && (
          <>
            <span className="flex-1 text-left">{item.name}</span>
            <ChevronDownIcon className={`h-4 w-4 text-fd-mute transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
          </>
        )}
      </button>

      {!collapsed && isOpen && visibleChildren && visibleChildren.length > 0 && (
        <div className="mt-0.5 ml-[18px] pl-3 border-l border-fd-line space-y-px">
          {visibleChildren.map(child => {
            const isChildActive = isHrefActive(child.href, location);
            return (
              <Link
                key={child.name}
                to={child.href!}
                className={`
                  flex items-center gap-2.5 px-3 py-1.5 rounded-[3px]
                  text-[12.5px] transition-all duration-150
                  ${
                    isChildActive
                      ? 'bg-[rgba(47,129,247,0.1)] text-fd-ink font-medium shadow-[inset_2px_0_0_#2f81f7]'
                      : 'text-fd-mute hover:bg-white/[0.03] hover:text-fd-body'
                  }
                `}
                onClick={onNavigate}
              >
                <child.icon className={`h-4 w-4 flex-shrink-0 ${isChildActive ? 'text-fd-blue' : ''}`} />
                <span>{child.name}</span>
                {child.badge && (
                  <span className="ml-auto bg-fd-amber text-slate-950 text-[10px] font-bold font-mono px-1.5 py-0.5 rounded-[3px]">
                    {child.badge}
                  </span>
                )}
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

// Foundry HUD live clock (24h)
const HudClock = React.memo(function HudClock() {
  const [t, setT] = useState('');
  useEffect(() => {
    const tick = () => setT(new Date().toLocaleTimeString('en-GB'));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="text-fd-ink tabular-nums">{t}</span>;
});

export default function Layout({ children }: LayoutProps) {
  const { user, logout, logoutWithEmployeeId } = useAuth();
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [logoutModalOpen, setLogoutModalOpen] = useState(false);
  const [logoutEmployeeId, setLogoutEmployeeId] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const [pendingApprovalCount, setPendingApprovalCount] = useState(0);
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

  }, [location.pathname, location.search]);

  const isShopFloorKiosk = useMemo(
    () =>
      location.pathname.startsWith('/shop-floor') &&
      isKioskMode(location.pathname, location.search) &&
      user?.role === 'operator',
    [location.pathname, location.search, user?.role]
  );

  const isOperator = user?.role === 'operator';
  const isAdminUser = user?.role === 'admin' || user?.role === 'platform_admin' || user?.is_superuser === true;
  const isPlatformAdmin = user?.role === 'platform_admin' || user?.is_superuser === true;
  const operatorDisplayName = useMemo(() => {
    const name = [user?.first_name, user?.last_name].filter(Boolean).join(' ').trim();
    return name || user?.email || 'Operator';
  }, [user?.email, user?.first_name, user?.last_name]);
  const operatorInitials = useMemo(() => {
    const initials = `${user?.first_name?.[0] || ''}${user?.last_name?.[0] || ''}`.trim();
    return initials || operatorDisplayName.slice(0, 2).toUpperCase();
  }, [operatorDisplayName, user?.first_name, user?.last_name]);

  const visibleNavigation = useMemo(() => {
    const withApprovalBadge = navigation.map((item) => {
      if (item.name !== 'Administration' || !item.children) return item;

      return {
        ...item,
        children: item.children.map((child) =>
          child.name === 'User Approvals'
            ? { ...child, badge: pendingApprovalCount > 0 ? pendingApprovalCount : undefined }
            : child
        ),
      };
    });

    if (isKiosk) {
      return withApprovalBadge.filter(item => item.name === 'Shop Floor');
    }

    // Operators see a streamlined nav: Shop Floor, Quality, and Downtime
    if (isOperator) {
      const operatorAllowed = new Set(['Dashboard', 'Shop Floor', 'Quality', 'Maintenance']);
      return withApprovalBadge.filter(item => operatorAllowed.has(item.name));
    }

    return withApprovalBadge;
  }, [isKiosk, isOperator, pendingApprovalCount]);

  useEffect(() => {
    if (!isAdminUser) {
      setPendingApprovalCount(0);
      return;
    }

    let cancelled = false;
    const loadPendingApprovalCount = async () => {
      try {
        const summary = await api.getPendingUserApprovalSummary();
        if (!cancelled) {
          setPendingApprovalCount(summary.count || 0);
        }
      } catch {
        if (!cancelled) {
          setPendingApprovalCount(0);
        }
      }
    };

    loadPendingApprovalCount();
    const interval = setInterval(loadPendingApprovalCount, 60000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [isAdminUser, location.pathname, location.search]);

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
    <div className="min-h-screen" style={{ background: 'var(--fd-canvas)' }}>
      {/* Skip to main content link for accessibility */}
      <SkipLink />

      {/* Mobile sidebar backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/60 backdrop-blur-sm lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar - Foundry instrument rail */}
      <aside
        className={`
          fixed inset-y-0 left-0 z-50 w-72
          transform transition-transform duration-300 ease-out
          lg:translate-x-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          flex flex-col overflow-hidden
        `}
        style={{
          background: 'var(--fd-panel)',
          borderRight: '1px solid var(--fd-line)',
        }}
      >
        {/* Blueprint grid background */}
        <SidebarPattern />

        {/* Logo header */}
        <div className="relative flex items-center justify-between h-14 px-4 flex-shrink-0" style={{ borderBottom: '1px solid var(--fd-line)' }}>
          <Link to="/" className="flex items-center gap-3">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-6 w-auto brightness-0 invert" />
          </Link>
          <div className="flex items-center gap-2">
            <span className="hidden lg:inline-flex items-center gap-1.5 font-mono text-[10px] tracking-[0.08em] text-fd-green px-1.5 py-0.5 rounded-[3px]" style={{ border: '1px solid var(--fd-line)' }}>
              <span className="w-1.5 h-1.5 rounded-full bg-fd-green shadow-[0_0_5px_#3fb950]" />
              LIVE
            </span>
            <button
              onClick={() => setSidebarOpen(false)}
              className="lg:hidden p-2 rounded-[3px] text-fd-mute hover:text-fd-ink hover:bg-white/5 transition-colors"
              aria-label="Close navigation menu"
            >
              <XMarkIcon className="h-6 w-6" aria-hidden="true" />
            </button>
          </div>
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
              isAdmin={isAdminUser}
              isPlatformAdmin={isPlatformAdmin}
            />
          ))}
        </nav>

        {/* Certifications strip */}
        <div className="relative flex-shrink-0 px-4 py-2" style={{ borderTop: '1px solid var(--fd-line)' }}>
          <div className="flex items-center justify-center gap-2 text-[9px] font-mono uppercase tracking-[0.14em] text-fd-faint">
            <span>AS9100D</span>
            <span>·</span>
            <span>ISO 9001</span>
            <span>·</span>
            <span>ITAR</span>
          </div>
        </div>

        {/* User section */}
        <div className="relative flex-shrink-0 px-3.5 py-2.5" style={{ borderTop: '1px solid var(--fd-line)' }}>
          <div className="flex items-center gap-2.5">
            <div className="flex-shrink-0">
              <div
                className="h-[30px] w-[30px] rounded-[3px] flex items-center justify-center text-fd-blue font-bold font-mono text-xs"
                style={{ background: 'var(--fd-raised)', border: '1px solid var(--fd-line-bright)' }}
              >
                {user?.first_name?.[0]}
                {user?.last_name?.[0]}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-[12.5px] font-semibold text-fd-ink truncate">
                {user?.first_name} {user?.last_name}
              </p>
              <p className="text-[10px] font-mono tracking-[0.05em] uppercase text-fd-mute truncate">{user?.role?.replace('_', ' ')}</p>
            </div>
            <button
              onClick={() => {
                if (isShopFloorKiosk) {
                  setLogoutModalOpen(true);
                } else {
                  logout();
                }
              }}
              className="p-1.5 rounded-[3px] text-fd-mute hover:text-fd-ink hover:bg-white/5 transition-all duration-150"
              title="Sign out"
            >
              <ArrowRightOnRectangleIcon className="h-[18px] w-[18px]" />
            </button>
          </div>
        </div>
      </aside>

      {/* Main content area */}
      <div className="lg:pl-72 flex flex-col min-h-screen">
        {/* Top bar - Foundry HUD command bar */}
        <header
          className="sticky top-0 z-30 backdrop-blur-md"
          style={{ background: 'rgba(12,16,23,0.92)', borderBottom: '1px solid var(--fd-line)' }}
        >
          <div className="flex items-center justify-between h-14 px-4 sm:px-6 lg:px-8">
            {/* Mobile menu button */}
            <button
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden p-2 -ml-2 rounded-[3px] text-fd-mute hover:bg-white/5 transition-colors"
            >
              <Bars3Icon className="h-6 w-6" />
            </button>

            {/* Breadcrumb (desktop) */}
            <div className="hidden lg:block font-mono text-xs tracking-[0.04em] whitespace-nowrap">
              <span className="text-fd-mute">WERCO / </span>
              <span className="text-fd-ink">{pageTitle}</span>
            </div>

            {/* Mobile logo / kiosk operator identity */}
            <div className="lg:hidden flex-1 min-w-0 px-3">
              {isShopFloorKiosk ? (
                <div className="flex items-center justify-center gap-2 min-w-0">
                  <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-werco-navy-600 to-blue-700 flex items-center justify-center text-white font-semibold text-sm shadow-md flex-shrink-0">
                    {operatorInitials}
                  </div>
                  <div className="min-w-0">
                    <p className="text-[11px] leading-3 font-medium uppercase tracking-wide text-slate-400">Signed in</p>
                    <p className="text-sm leading-5 font-semibold text-slate-100 truncate">{operatorDisplayName}</p>
                  </div>
                </div>
              ) : (
                <div className="flex justify-center">
                  <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-10 w-auto brightness-0 invert" />
                </div>
              )}
            </div>

            {/* Right side actions */}
            <div className="flex items-center gap-2" data-tour="user-menu">
              {isShopFloorKiosk && (
                <button
                  onClick={() => setLogoutModalOpen(true)}
                  className="lg:hidden inline-flex items-center gap-1.5 min-h-11 px-3 rounded-xl text-sm font-semibold text-white bg-red-600 hover:bg-red-500 active:bg-red-700 transition-colors shadow-sm"
                  aria-label="Sign out"
                >
                  <ArrowRightOnRectangleIcon className="h-5 w-5" />
                  <span>Sign out</span>
                </button>
              )}

              <div className={`${isShopFloorKiosk ? 'hidden lg:flex' : 'flex'} items-center gap-2`}>
                {/* Company switcher (platform admins) */}
                <CompanySwitcher />

                {/* Quick search button */}
                <button
                  onClick={globalSearch.open}
                  className="flex items-center gap-2 px-3 h-[34px] rounded-[3px] text-fd-mute hover:text-fd-body transition-all duration-150"
                  style={{ background: 'var(--fd-sunken)', border: '1px solid var(--fd-line)' }}
                  title="Search (Ctrl+K)"
                  data-tour="search"
                >
                  <MagnifyingGlassIcon className="h-4 w-4" />
                  <span className="hidden md:inline font-mono text-xs">search</span>
                  <kbd className="hidden md:inline-flex items-center px-1.5 py-0.5 font-mono text-[10px] text-fd-faint rounded-[3px]" style={{ border: '1px solid var(--fd-line)' }}>
                    /
                  </kbd>
                </button>

                {/* HUD status cluster */}
                <div className="hidden xl:flex items-center gap-3.5 pl-1 font-mono text-[11px]">
                  <div>
                    <span className="text-fd-faint">SYNC </span>
                    <span className="text-fd-green">OK</span>
                  </div>
                  <div>
                    <span className="text-fd-faint">SHIFT </span>
                    <span className="text-fd-ink">A</span>
                  </div>
                  <HudClock />
                </div>

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
          </div>
        </header>

        {/* Read-only banner when viewing another company */}
        <ReadOnlyBanner />

        {/* Main content - extra bottom padding for mobile nav */}
        <main id="main-content" className="relative flex-1 p-4 sm:p-6 lg:p-8 pb-20 lg:pb-8" role="main" tabIndex={-1}>
          <div
            className="pointer-events-none absolute inset-0 opacity-40"
            aria-hidden="true"
            style={{
              backgroundImage:
                'linear-gradient(rgba(36,48,68,.25) 1px,transparent 1px),linear-gradient(90deg,rgba(36,48,68,.25) 1px,transparent 1px)',
              backgroundSize: '28px 28px',
            }}
          />
          <div className="relative max-w-7xl mx-auto animate-fade-in">{children}</div>
        </main>

        {/* Footer - Hidden on mobile, visible on desktop */}
        <footer className="hidden lg:block flex-shrink-0 py-3 px-6" style={{ borderTop: '1px solid var(--fd-line)', background: 'var(--fd-sunken)' }}>
          <div className="max-w-7xl mx-auto flex items-center justify-between text-sm text-fd-mute">
            <div className="flex items-center gap-2 font-mono text-xs">
              <span className="font-medium text-fd-body">WERCO MANUFACTURING</span>
              <span className="text-fd-faint">·</span>
              <span className="text-fd-blue font-semibold">ERP / MES</span>
            </div>
            <div className="flex items-center gap-3 font-mono">
              <span className="text-[10px] uppercase tracking-[0.14em] text-fd-faint">AS9100D &middot; ISO 9001 &middot; ITAR</span>
              <span className="text-fd-faint">·</span>
              <span className="text-[10px] text-fd-mute">v1.0.0</span>
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

      {!isKiosk && <AdaptivePromptPanel />}

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
