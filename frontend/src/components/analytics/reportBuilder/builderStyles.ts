/** Shared class strings for the report builder's dense instrument-panel rows. */

/** `.input` without its 44px touch height: builder rows are a desk tool, not a kiosk. */
export const COMPACT_INPUT = 'input min-h-0 py-1.5 px-2.5 text-sm';

/** A square icon-only row action (move / remove). Always pair with an aria-label. */
export const ICON_BUTTON =
  'inline-flex h-8 w-8 items-center justify-center rounded-sm border border-fd-line text-fd-mute ' +
  'hover:border-fd-line-bright hover:text-fd-ink disabled:cursor-not-allowed disabled:opacity-40';

/** A fieldset section inside the builder card. */
export const SECTION = 'border-t border-fd-line pt-3';

/** The fieldset legend for a SECTION. */
export const LEGEND = 'text-xs font-semibold uppercase tracking-wide text-fd-mute';
