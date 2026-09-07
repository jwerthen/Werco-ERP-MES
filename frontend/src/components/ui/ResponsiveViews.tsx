import React, { useEffect, useState } from 'react';

interface ResponsiveViewsProps {
  desktop: React.ReactNode;
  mobile: React.ReactNode;
}

/** Match Tailwind's lg breakpoint without mounting the invisible list too. */
export function ResponsiveViews({ desktop, mobile }: ResponsiveViewsProps) {
  // Read the viewport during the initial client render: a phone never mounts
  // the desktop table first. The server fallback is unused in this Vite SPA.
  const [isDesktop, setIsDesktop] = useState(() => typeof window === 'undefined' || window.innerWidth >= 1024);
  useEffect(() => {
    const update = () => setIsDesktop(window.innerWidth >= 1024);
    update();
    window.addEventListener('resize', update, { passive: true });
    return () => window.removeEventListener('resize', update);
  }, []);
  return <>{isDesktop ? desktop : mobile}</>;
}
