import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import {
  HomeIcon,
  ClipboardDocumentListIcon,
  CubeIcon,
  Bars3Icon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import {
  HomeIcon as HomeIconSolid,
  ClipboardDocumentListIcon as ClipboardDocumentListIconSolid,
  CubeIcon as CubeIconSolid,
  WrenchScrewdriverIcon as WrenchScrewdriverIconSolid,
} from '@heroicons/react/24/solid';

interface NavItem {
  name: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  activeIcon: React.ComponentType<{ className?: string }>;
}

const defaultNavItems: NavItem[] = [
  {
    name: 'Home',
    href: '/',
    icon: HomeIcon,
    activeIcon: HomeIconSolid,
  },
  {
    name: 'Shop Floor',
    href: '/shop-floor',
    icon: WrenchScrewdriverIcon,
    activeIcon: WrenchScrewdriverIconSolid,
  },
  {
    name: 'Work Orders',
    href: '/work-orders',
    icon: ClipboardDocumentListIcon,
    activeIcon: ClipboardDocumentListIconSolid,
  },
  {
    name: 'Parts',
    href: '/parts',
    icon: CubeIcon,
    activeIcon: CubeIconSolid,
  },
];

const operatorNavItems: NavItem[] = [
  {
    name: 'My Jobs',
    href: '/shop-floor',
    icon: WrenchScrewdriverIcon,
    activeIcon: WrenchScrewdriverIconSolid,
  },
  {
    name: 'Operations',
    href: '/shop-floor/operations',
    icon: ClipboardDocumentListIcon,
    activeIcon: ClipboardDocumentListIconSolid,
  },
];

interface BottomNavProps {
  onMenuClick: () => void;
}

export default function BottomNav({ onMenuClick }: BottomNavProps) {
  const location = useLocation();
  const { user } = useAuth();
  const isOperator = user?.role === 'operator';
  const navItems = isOperator ? operatorNavItems : defaultNavItems;

  const isActive = (href: string) => {
    if (href === '/') return location.pathname === '/';
    return location.pathname.startsWith(href);
  };

  return (
    <nav className="fixed bottom-0 left-0 right-0 z-40 bg-white border-t border-slate-200 pb-safe lg:hidden">
      <div className="flex items-center justify-around h-16">
        {navItems.map((item) => {
          const active = isActive(item.href);
          const Icon = active ? item.activeIcon : item.icon;
          
          return (
            <Link
              key={item.name}
              to={item.href}
              className={`
                flex flex-col items-center justify-center flex-1 h-full
                transition-colors duration-200
                ${active ? 'text-werco-navy-600' : 'text-slate-500'}
              `}
            >
              <Icon className="h-6 w-6" />
              <span className="text-xs mt-1 font-medium">{item.name}</span>
            </Link>
          );
        })}
        
        {/* More menu button */}
        <button
          onClick={onMenuClick}
          className="flex flex-col items-center justify-center flex-1 h-full text-slate-500 active:text-slate-700"
        >
          <Bars3Icon className="h-6 w-6" />
          <span className="text-xs mt-1 font-medium">More</span>
        </button>
      </div>
    </nav>
  );
}
