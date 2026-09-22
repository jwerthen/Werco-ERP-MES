import React from 'react';

/** A small, code-native yellow Labrador mark for Hank's shared UI identity. */
export function HankAvatar({ className = 'h-9 w-9' }: { className?: string }) {
  return (
    <svg viewBox="0 0 48 48" className={className} aria-hidden="true" focusable="false">
      <rect width="48" height="48" rx="10" fill="#24364d" />
      <path d="M13 29c-5 0-7-5-6-11 1-5 5-9 10-8l-1 17-3 2Zm22 0c5 0 7-5 6-11-1-5-5-9-10-8l1 17 3 2Z" fill="#cea15b" />
      <path d="M12 21c0-9 4-14 12-14s12 5 12 14v8c0 8-6 13-12 13s-12-5-12-13v-8Z" fill="#e9c987" />
      <path d="M15 30c0-5 4-8 9-8s9 3 9 8-4 9-9 9-9-4-9-9Z" fill="#f4deb0" />
      <circle cx="17" cy="21" r="1.8" fill="#2b2c2d" />
      <circle cx="31" cy="21" r="1.8" fill="#2b2c2d" />
      <path d="M20 28c0-2 8-2 8 0 0 3-3 4-4 4s-4-1-4-4Z" fill="#333335" />
      <path d="M24 32v3m-4 0c2 2 6 2 8 0" fill="none" stroke="#715439" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M16 39c5 3 11 3 16 0l-1 5H17l-1-5Z" fill="#c8352b" />
      <circle cx="24" cy="43" r="2.3" fill="#e9c987" />
    </svg>
  );
}
