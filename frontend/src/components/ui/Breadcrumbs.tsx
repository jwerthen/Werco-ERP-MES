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
    <nav
      className="flex items-center font-mono text-xs tracking-[0.04em] text-fd-mute mb-1"
      aria-label="Breadcrumb"
    >
      {crumbs.map((crumb, idx) => (
        <React.Fragment key={idx}>
          {idx > 0 && <ChevronRightIcon className="h-3 w-3 mx-1.5 text-fd-faint flex-shrink-0" />}
          {crumb.href ? (
            <Link to={crumb.href} className="hover:text-fd-blue transition-colors">
              {crumb.label}
            </Link>
          ) : (
            <span className="text-fd-ink font-medium truncate">{crumb.label}</span>
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}
