/**
 * 404 Not Found page.
 *
 * Instrument-panel chrome (fd-* tokens, hairline border, sharp corners) and an
 * SPA `<Link>` home — a raw `<a href="/">` here would force a full app reload
 * on what is otherwise an in-app wrong turn.
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { usePageTitle } from '../hooks/usePageTitle';

export default function NotFound() {
  // Renders outside Layout (catch-all route), so the tab title is set here —
  // otherwise the previous page's title lingers over the 404.
  usePageTitle('Page Not Found · Werco ERP');

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-4">
      <div className="w-full max-w-md rounded-sm border border-fd-line bg-fd-panel p-8 text-center">
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-fd-mute">Error 404</p>
        <h1 className="mt-2 text-2xl font-bold text-fd-ink">Page not found</h1>
        <p className="mt-2 text-sm text-fd-body">
          The page you are looking for does not exist or may have moved.
        </p>
        <Link to="/" className="btn-primary mt-6 inline-flex items-center justify-center">
          Back to Dashboard
        </Link>
      </div>
    </div>
  );
}
