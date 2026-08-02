/**
 * usePageTitle — set the browser-tab title (`document.title`) for the current
 * screen.
 *
 * No restore-on-unmount: routed screens each set their own title, so the next
 * route simply overwrites the previous one — a cleanup would only flash the
 * default title between routes. Print-oriented pages are deliberately excluded
 * from adopting this hook: browsers use `document.title` as the default
 * print / save-as-PDF filename, and those pages must keep their own naming.
 */
import { useEffect } from 'react';

export function usePageTitle(title: string): void {
  useEffect(() => {
    document.title = title;
  }, [title]);
}

export default usePageTitle;
