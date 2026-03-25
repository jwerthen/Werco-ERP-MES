import React from 'react';
import { Link } from 'react-router-dom';
import { ChevronRightIcon } from '@heroicons/react/24/outline';

export interface Crumb {
  label: string;
  href?: string;
}

interface BreadcrumbsProps {
  crumbs: Crumb[];
}

export function Breadcrumbs({ crumbs }: BreadcrumbsProps) {
  return (
    <nav className="flex items-center text-sm text-gray-500 mb-1" aria-label="Breadcrumb">
      {crumbs.map((crumb, idx) => (
        <React.Fragment key={idx}>
          {idx > 0 && <ChevronRightIcon className="h-3.5 w-3.5 mx-1.5 text-gray-400 flex-shrink-0" />}
          {crumb.href ? (
            <Link to={crumb.href} className="hover:text-werco-navy-600 transition-colors">
              {crumb.label}
            </Link>
          ) : (
            <span className="text-gray-900 font-medium">{crumb.label}</span>
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}
