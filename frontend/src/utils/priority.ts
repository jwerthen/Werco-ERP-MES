/** Work-order priority: lower numbers are more urgent across every planning view. */
export const PRIORITY_HELP_TEXT = 'P1 is most urgent; P10 is least urgent.';
const names = ['Critical', 'Urgent', 'High', 'Elevated', 'Normal', 'Low', 'Low', 'Low', 'Lowest', 'Lowest'];
export function getPriorityLabel(priority: number): string {
  return Number.isInteger(priority) && priority >= 1 && priority <= 10
    ? `P${priority} · ${names[priority - 1]}`
    : 'Priority unavailable';
}
export function getPriorityTone(priority: number): 'critical' | 'high' | 'normal' | 'low' {
  return priority <= 2 ? 'critical' : priority <= 4 ? 'high' : priority === 5 ? 'normal' : 'low';
}
export function getPriorityClasses(priority: number): string {
  return {
    critical: 'bg-red-500/15 text-red-300 border-red-500/30',
    high: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
    normal: 'bg-blue-500/15 text-blue-300 border-blue-500/30',
    low: 'bg-slate-500/15 text-slate-300 border-slate-500/30',
  }[getPriorityTone(priority)];
}
export const PRIORITY_OPTIONS = Array.from({ length: 10 }, (_, index) => ({
  value: index + 1,
  label: getPriorityLabel(index + 1),
}));
