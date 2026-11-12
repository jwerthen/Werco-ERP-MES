import { useEffect, useState } from 'react';

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

/**
 * True while the viewer asks for reduced motion (OS / browser setting), tracking changes
 * live. Mirrors `usePhoneLayout`. Used to switch off animation the CSS reduced-motion rule
 * cannot reach — the Web Animations API (dashboard FLIP, the drag drop animation) and
 * smooth scrolling.
 */
export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(() => window.matchMedia(REDUCED_MOTION_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(REDUCED_MOTION_QUERY);
    const update = () => setReduced(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return reduced;
}
