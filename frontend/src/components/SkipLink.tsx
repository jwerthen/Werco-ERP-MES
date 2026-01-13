/**
 * SkipLink Component
 * 
 * Provides a skip navigation link for keyboard users and screen readers.
 * WCAG 2.1 AA compliant - allows users to skip to main content.
 */

import React from 'react';

interface SkipLinkProps {
  /** ID of the main content element to skip to (default: "main-content") */
  targetId?: string;
  /** Custom text for the skip link */
  text?: string;
}

export function SkipLink({ 
  targetId = 'main-content', 
  text = 'Skip to main content' 
}: SkipLinkProps) {
  return (
    <a
      href={`#${targetId}`}
      className="
        sr-only focus:not-sr-only
        focus:fixed focus:top-4 focus:left-4 focus:z-[100]
        focus:px-4 focus:py-2 
        focus:bg-cyan-600 focus:text-white 
        focus:rounded-lg focus:shadow-lg
        focus:outline-none focus:ring-2 focus:ring-cyan-400 focus:ring-offset-2
        transition-all duration-200
      "
    >
      {text}
    </a>
  );
}

export default SkipLink;
