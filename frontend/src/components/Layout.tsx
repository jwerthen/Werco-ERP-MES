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
  CalendarDaysIcon,
  DocumentTextIcon,
  ChartBarIcon,
  PaperAirplaneIcon,
  CurrencyDollarIcon,
  UsersIcon,
  BuildingOfficeIcon,
  QrCodeIcon,
  WrenchScrewdriverIcon as WrenchIcon2,
  DocumentMagnifyingGlassIcon,
  ChevronDownIcon,
  Cog6ToothIcon,
  MagnifyingGlassIcon,
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
    ]
  },
  { name: 'Scanner', href: '/scanner', icon: QrCodeIcon },
  { name: 'Scheduling', href: '/scheduling', icon: CalendarDaysIcon },
  { name: 'Work Orders', href: '/work-orders', icon: ClipboardDocumentListIcon },
  { 
    name: 'Engineering', 
    icon: CubeIcon,
    children: [
      { name: 'Parts', href: '/parts', icon: CubeIcon },
      { name: 'Bill of Materials', href: '/bom', icon: DocumentDuplicateIcon },
      { name: 'Routing', href: '/routing', icon: ListBulletIcon },
    ]
  },
  { 
    name: 'Inventory & Purchasing', 
    icon: ArchiveBoxIcon,
    children: [
      { name: 'Inventory', href: '/inventory', icon: ArchiveBoxIcon },
      { name: 'Purchasing', href: '/purchasing', icon: TruckIcon },
      { name: 'Upload PO', href: '/po-upload', icon: DocumentDuplicateIcon },
      { name: 'Receiving', href: '/receiving', icon: TruckIcon },
      { name: 'MRP', href: '/mrp', icon: CalculatorIcon },
    ]
  },
  { 
    name: 'Sales & Shipping', 
    icon: PaperAirplaneIcon,
    children: [
      { name: 'Quote Calculator', href: '/quote-calculator', icon: CalculatorIcon },
      { name: 'Quotes', href: '/quotes', icon: CurrencyDollarIcon },
      { name: 'Shipping', href: '/shipping', icon: PaperAirplaneIcon },
      { name: 'Customers', href: '/customers', icon: BuildingOfficeIcon },
    ]
  },
  { 
    name: 'Quality', 
    icon: ShieldCheckIcon,
    children: [
      { name: 'NCR / CAR / FAI', href: '/quality', icon: ShieldCheckIcon },
      { name: 'Calibration', href: '/calibration', icon: WrenchIcon2 },
      { name: 'Traceability', href: '/traceability', icon: DocumentMagnifyingGlassIcon },
    ]
  },
  { name: 'Documents', href: '/documents', icon: DocumentTextIcon },
  { name: 'Analytics', href: '/analytics', icon: ChartBarIcon },
  { name: 'Reports', href: '/reports', icon: DocumentTextIcon },
  { 
    name: 'Administration', 
    icon: Cog6ToothIcon,
    children: [
      { name: 'Work Centers', href: '/work-centers', icon: CogIcon },
      { name: 'Users', href: '/users', icon: UsersIcon },
      { name: 'Custom Fields', href: '/custom-fields', icon: AdjustmentsHorizontalIcon },
      { name: 'Admin Settings', href: '/admin/settings', icon: Cog6ToothIcon, adminOnly: true },
      { name: 'Audit Log', href: '/audit-log', icon: ShieldCheckIcon },
    ]
  },
];

const NavGroup = React.memo(function NavGroup({ item, location, onNavigate, collapsed, isAdmin }: { 
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
      return visibleChildren.some(child => location.pathname === child.href);
    }
    return false;
  });

  // Auto-open when navigating to a child
  useEffect(() => {
    if (visibleChildren?.some(child => location.pathname === child.href)) {
      setIsOpen(true);
    }
  }, [location.pathname, visibleChildren]);

  const isActive = item.href === location.pathname;
  const hasActiveChild = visibleChildren?.some(child => location.pathname === child.href);

  if (item.href) {
    return (
      <Link
        to={item.href}
        className={`
          group flex items-center gap-3 px-3 py-2.5 rounded-xl
          text-sm font-medium transition-all duration-200
          ${isActive 
            ? 'bg-white/15 text-white shadow-sm' 
            : 'text-white/70 hover:bg-white/10 hover:text-white'
          }
        `}
        onClick={onNavigate}
        title={collapsed ? item.name : undefined}
      >
        <item.icon className={`h-5 w-5 flex-shrink-0 transition-colors ${isActive ? 'text-white' : 'text-white/60 group-hover:text-white'}`} />
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
          ${hasActiveChild 
            ? 'bg-white/10 text-white' 
            : 'text-white/70 hover:bg-white/10 hover:text-white'
          }
        `}
        title={collapsed ? item.name : undefined}
      >
        <item.icon className={`h-5 w-5 flex-shrink-0 transition-colors ${hasActiveChild ? 'text-white' : 'text-white/60 group-hover:text-white'}`} />
        {!collapsed && (
          <>
            <span className="flex-1 text-left">{item.name}</span>
            <ChevronDownIcon 
              className={`h-4 w-4 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} 
            />
          </>
        )}
      </button>
      
      {!collapsed && isOpen && visibleChildren && visibleChildren.length > 0 && (
        <div className="mt-1 ml-3 pl-3 border-l-2 border-white/20 space-y-0.5">
          {visibleChildren.map((child) => {
            const isChildActive = location.pathname === child.href;
            return (
              <Link
                key={child.name}
                to={child.href!}
                className={`
                  flex items-center gap-2.5 px-3 py-2 rounded-lg
                  text-sm transition-all duration-200
                  ${isChildActive 
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
// Hexagon grid pattern for sidebar
const SidebarPattern = () => (
  <svg className="absolute inset-0 w-full h-full opacity-[0.03]" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <pattern id="sidebar-hex" width="56" height="100" patternUnits="userSpaceOnUse" patternTransform="scale(1.5)">
        <path d="M28 66L0 50L0 16L28 0L56 16L56 50L28 66L28 100" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-cyan-300"/>
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#sidebar-hex)" />
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
      if (item.href === location.pathname) return item.name;
      if (item.children) {
        const child = item.children.find(c => c.href === location.pathname);
        if (child) return child.name;
      }
    }
    return 'Werco ERP';
  }, [location.pathname]);

  const isShopFloorKiosk = useMemo(
    () => location.pathname.startsWith('/shop-floor') && isKioskMode(location.search),
    [location.pathname, location.search]
  );

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
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-slate-100 to-blue-50">
      {/* Skip to main content link for accessibility */}
      <SkipLink />
      
      {/* Mobile sidebar backdrop */}
      {sidebarOpen && (
        <div 
          className="fixed inset-0 z-40 bg-slate-900/60 backdrop-blur-sm lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar - Dark premium style */}
      <aside 
        className={`
          fixed inset-y-0 left-0 z-50 w-72 
          bg-gradient-to-b from-slate-900 via-slate-900 to-blue-950
          transform transition-transform duration-300 ease-out
          lg:translate-x-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          flex flex-col overflow-hidden
        `}
      >
        {/* Animated background elements */}
        <SidebarPattern />
        <div className="absolute top-1/4 -left-20 w-40 h-40 bg-cyan-500/10 rounded-full blur-3xl" />
        <div className="absolute bottom-1/4 -right-20 w-60 h-60 bg-blue-500/10 rounded-full blur-3xl" />
        
        {/* Logo header */}
        <div className="relative flex items-center justify-between h-20 px-4 border-b border-white/10 flex-shrink-0">
          <Link to="/" className="flex items-center gap-3">
            <img 
              src="/Werco_Logo-PNG.png" 
              alt="Werco Manufacturing" 
              className="h-12 w-auto brightness-0 invert" 
            />
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
          {navigation.map((item) => (
            <NavGroup 
              key={item.name} 
              item={item} 
              location={location} 
              onNavigate={() => setSidebarOpen(false)}
              isAdmin={user?.role === 'admin'}
            />
          ))}
        </nav>

        {/* User section */}
        <div className="relative flex-shrink-0 p-4 border-t border-white/10 bg-white/5">
          <div className="flex items-center gap-3">
            <div className="flex-shrink-0">
              <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-cyan-500 to-cyan-600 flex items-center justify-center text-white font-semibold text-sm shadow-lg">
                {user?.first_name?.[0]}{user?.last_name?.[0]}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white truncate">
                {user?.first_name} {user?.last_name}
              </p>
              <p className="text-xs text-cyan-400/80 truncate capitalize">
                {user?.role?.replace('_', ' ')}
              </p>
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
        {/* Top bar - Glassmorphism style */}
        <header className="sticky top-0 z-30 bg-white/80 backdrop-blur-xl border-b border-white/50 shadow-sm">
          <div className="flex items-center justify-between h-16 px-4 sm:px-6 lg:px-8">
            {/* Mobile menu button */}
            <button
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden p-2 -ml-2 rounded-xl text-slate-600 hover:bg-slate-100 transition-colors"
            >
              <Bars3Icon className="h-6 w-6" />
            </button>

            {/* Page title (desktop) */}
            <div className="hidden lg:block">
              <h1 className="text-lg font-semibold text-slate-800">{pageTitle}</h1>
            </div>

            {/* Mobile logo */}
            <div className="lg:hidden">
              <img 
                src="/Werco_Logo-PNG.png" 
                alt="Werco Manufacturing" 
                className="h-10 w-auto" 
              />
            </div>

            {/* Right side actions */}
            <div className="flex items-center gap-2" data-tour="user-menu">
              {/* Quick search button */}
              <button
                onClick={globalSearch.open}
                className="flex items-center gap-2 px-3 py-2 rounded-xl text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-all duration-200 border border-slate-200"
                title="Search (Ctrl+K)"
                data-tour="search"
              >
                <MagnifyingGlassIcon className="h-4 w-4" />
                <span className="hidden md:inline text-sm">Search...</span>
                <kbd className="hidden md:inline-flex items-center px-1.5 py-0.5 text-xs font-medium text-slate-400 bg-slate-100 rounded">
                  Ctrl+K
                </kbd>
              </button>

              {/* Keyboard shortcuts help */}
              <button
                onClick={keyboardShortcuts.showHelp}
                className="hidden md:flex items-center justify-center p-2 rounded-xl text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-all duration-200"
                title="Keyboard shortcuts (Ctrl+/)"
                aria-label="Show keyboard shortcuts"
              >
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
                </svg>
              </button>

              {/* Help & Tours menu */}
              <div className="hidden lg:block">
                <TourMenu />
              </div>

              {/* User avatar (mobile) */}
              <div className="lg:hidden flex items-center">
                <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-cyan-500 to-cyan-600 flex items-center justify-center text-white font-medium text-sm shadow-md">
                  {user?.first_name?.[0]}{user?.last_name?.[0]}
                </div>
              </div>
            </div>
          </div>
        </header>

        {/* Main content - extra bottom padding for mobile nav */}
        <main 
          id="main-content" 
          className="flex-1 p-4 sm:p-6 lg:p-8 pb-20 lg:pb-8"
          role="main"
          tabIndex={-1}
        >
          <div className="max-w-7xl mx-auto animate-fade-in">
            {children}
          </div>
        </main>

        {/* Footer - Hidden on mobile, visible on desktop */}
        <footer className="hidden lg:block flex-shrink-0 py-4 px-6 border-t border-white/50 bg-white/60 backdrop-blur-sm">
          <div className="max-w-7xl mx-auto flex items-center justify-between text-sm text-slate-500">
            <div className="flex items-center gap-2">
              <span>Werco Manufacturing</span>
              <span className="text-slate-300">|</span>
              <span className="text-cyan-600 font-medium">MES</span>
            </div>
            <span className="text-slate-400">v1.0.0</span>
          </div>
        </footer>
      </div>

      {/* Bottom Navigation - Mobile only */}
      <BottomNav onMenuClick={() => setSidebarOpen(true)} />

      {/* Global Search Modal */}
      <GlobalSearch isOpen={globalSearch.isOpen} onClose={globalSearch.close} />

      {/* Session Warning Modal */}
      <SessionWarningModal />

      {/* Employee ID Logout Modal */}
      {logoutModalOpen && isShopFloorKiosk && (
        <div className="modal-overlay" onClick={() => setLogoutModalOpen(false)}>
          <div className="modal max-w-sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold text-slate-900">Sign out</h3>
              <button
                onClick={() => setLogoutModalOpen(false)}
                className="p-2 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100"
                aria-label="Close"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-3">
              <p className="text-sm text-slate-600">
                Enter your 4-digit employee ID to sign out.
              </p>
              <input
                type="text"
                inputMode="numeric"
                pattern="\\d{4}"
                maxLength={4}
                value={logoutEmployeeId}
                onChange={(e) => setLogoutEmployeeId(e.target.value.replace(/\\D/g, '').slice(0, 4))}
                className="input text-center text-lg tracking-widest"
                placeholder="0000"
                autoFocus
              />
              {logoutError && (
                <div className="text-sm text-red-600">{logoutError}</div>
              )}
            </div>

            <div className="modal-footer">
              <button
                onClick={() => {
                  setLogoutModalOpen(false);
                  setLogoutEmployeeId('');
                  setLogoutError('');
                }}
                className="btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleLogoutConfirm}
                className="btn-primary"
                disabled={logoutEmployeeId.length !== 4}
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

