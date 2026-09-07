import type React from 'react';
/** Automatic activation with Arrow/Home/End; only the selected tab is in the Tab order. */
export function tabKeyboard(event: React.KeyboardEvent<HTMLElement>) {
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
  const index = tabs.indexOf(event.target as HTMLButtonElement);
  if (index < 0) return;
  const next =
    event.key === 'ArrowRight'
      ? (index + 1) % tabs.length
      : event.key === 'ArrowLeft'
        ? (index - 1 + tabs.length) % tabs.length
        : event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? tabs.length - 1
            : null;
  if (next === null) return;
  event.preventDefault();
  tabs[next].click();
  tabs[next].focus();
}
