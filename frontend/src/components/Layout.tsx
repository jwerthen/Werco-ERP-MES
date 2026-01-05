import React, { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
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
  ChevronRightIcon,
  Cog6ToothIcon,
} from '@heroicons/react/24/outline';

interface LayoutProps {
  children: React.ReactNode;
}

interface NavItem {
  name: string;
  href?: string;
  icon: React.ComponentType<{ className?: string }>;
  children?: NavItem[];
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/', icon: HomeIcon },
  { name: 'Shop Floor', href: '/shop-floor', icon: WrenchScrewdriverIcon },
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
  { name: 'Reports', href: '/reports', icon: ChartBarIcon },
  { 
    name: 'Administration', 
    icon: Cog6ToothIcon,
    children: [
      { name: 'Work Centers', href: '/work-centers', icon: CogIcon },
      { name: 'Users', href: '/users', icon: UsersIcon },
      { name: 'Custom Fields', href: '/custom-fields', icon: AdjustmentsHorizontalIcon },
      { name: 'Audit Log', href: '/audit-log', icon: ShieldCheckIcon },
    ]
  },
];

function NavGroup({ item, location, onNavigate }: { item: NavItem; location: any; onNavigate?: () => void }) {
  const [isOpen, setIsOpen] = useState(() => {
    // Auto-open if current path is in this group
    if (item.children) {
      return item.children.some(child => location.pathname === child.href);
    }
    return false;
  });

  if (item.href) {
    // Single item
    return (
      <Link
        to={item.href}
        className={`flex items-center px-4 py-2.5 text-sm font-medium rounded-lg transition-colors ${
          location.pathname === item.href
            ? 'bg-blue-700 text-white'
            : 'text-blue-100 hover:bg-blue-700'
        }`}
        onClick={onNavigate}
      >
        <item.icon className="h-5 w-5 mr-3 flex-shrink-0" />
        {item.name}
      </Link>
    );
  }

  // Group with children
  return (
    <div>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`w-full flex items-center justify-between px-4 py-2.5 text-sm font-medium rounded-lg transition-colors ${
          isOpen ? 'bg-blue-800 text-white' : 'text-blue-100 hover:bg-blue-700'
        }`}
      >
        <div className="flex items-center">
          <item.icon className="h-5 w-5 mr-3 flex-shrink-0" />
          {item.name}
        </div>
        {isOpen ? (
          <ChevronDownIcon className="h-4 w-4" />
        ) : (
          <ChevronRightIcon className="h-4 w-4" />
        )}
      </button>
      {isOpen && item.children && (
        <div className="mt-1 ml-4 pl-4 border-l border-blue-600 space-y-1">
          {item.children.map((child) => (
            <Link
              key={child.name}
              to={child.href!}
              className={`flex items-center px-3 py-2 text-sm rounded-lg transition-colors ${
                location.pathname === child.href
                  ? 'bg-blue-700 text-white'
                  : 'text-blue-200 hover:bg-blue-700 hover:text-white'
              }`}
              onClick={onNavigate}
            >
              <child.icon className="h-4 w-4 mr-2 flex-shrink-0" />
              {child.name}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Layout({ children }: LayoutProps) {
  const { user, logout } = useAuth();
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="min-h-screen bg-gray-100">
      {/* Mobile sidebar */}
      <div className={`fixed inset-0 z-40 lg:hidden ${sidebarOpen ? '' : 'hidden'}`}>
        <div className="fixed inset-0 bg-gray-600 bg-opacity-75" onClick={() => setSidebarOpen(false)} />
        <div className="fixed inset-y-0 left-0 flex w-64 flex-col bg-werco-primary overflow-y-auto">
          <div className="flex h-16 items-center justify-between px-4 bg-white flex-shrink-0">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-12 w-auto" />
            <button onClick={() => setSidebarOpen(false)} className="text-gray-600">
              <XMarkIcon className="h-6 w-6" />
            </button>
          </div>
          <nav className="flex-1 px-2 py-4 space-y-1">
            {navigation.map((item) => (
              <NavGroup 
                key={item.name} 
                item={item} 
                location={location} 
                onNavigate={() => setSidebarOpen(false)}
              />
            ))}
          </nav>
        </div>
      </div>

      {/* Desktop sidebar */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-64 lg:flex-col">
        <div className="flex flex-col flex-grow bg-werco-primary overflow-y-auto">
          <div className="flex h-20 items-center justify-center px-4 bg-white flex-shrink-0">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-16 w-auto" />
          </div>
          <nav className="flex-1 px-2 py-4 space-y-1 overflow-y-auto">
            {navigation.map((item) => (
              <NavGroup key={item.name} item={item} location={location} />
            ))}
          </nav>
          <div className="p-4 border-t border-blue-700 flex-shrink-0">
            <div className="flex items-center">
              <div className="flex-shrink-0">
                <div className="h-10 w-10 rounded-full bg-blue-700 flex items-center justify-center text-white font-medium">
                  {user?.first_name?.[0]}{user?.last_name?.[0]}
                </div>
              </div>
              <div className="ml-3 flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">{user?.first_name} {user?.last_name}</p>
                <p className="text-xs text-blue-200 truncate">{user?.role}</p>
              </div>
              <button
                onClick={logout}
                className="text-blue-200 hover:text-white flex-shrink-0"
                title="Logout"
              >
                <ArrowRightOnRectangleIcon className="h-5 w-5" />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Main content */}
      <div className="lg:pl-64">
        {/* Top bar for mobile */}
        <div className="sticky top-0 z-10 flex h-16 bg-white shadow lg:hidden">
          <button
            onClick={() => setSidebarOpen(true)}
            className="px-4 text-gray-500 focus:outline-none"
          >
            <Bars3Icon className="h-6 w-6" />
          </button>
          <div className="flex flex-1 items-center justify-center">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-10 w-auto" />
          </div>
        </div>

        <main className="p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
