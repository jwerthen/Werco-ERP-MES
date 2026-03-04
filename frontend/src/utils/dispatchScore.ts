import { getCentralDateStamp, getCentralTodayISODate } from './centralTime';

interface DispatchScoreInput {
  priority: number;
  dueDate?: string | null;
  remainingHours?: number;
  scheduledStart?: string | null;
  status?: string | null;
}

const clampPriority = (priority: number) => {
  if (Number.isNaN(priority)) return 5;
  return Math.min(10, Math.max(1, priority));
};

const daysBetween = (targetDate: string, baseDate: string) => {
  const oneDay = 24 * 60 * 60 * 1000;
  return Math.floor((Date.parse(`${targetDate}T00:00:00Z`) - Date.parse(`${baseDate}T00:00:00Z`)) / oneDay);
};

export const calculateDispatchScore = ({
  priority,
  dueDate,
  remainingHours,
  scheduledStart,
  status,
}: DispatchScoreInput): number => {
  const normalizedPriority = clampPriority(priority);
  const today = getCentralTodayISODate();
  const due = dueDate ? getCentralDateStamp(dueDate) : '';

  let score = (11 - normalizedPriority) * 16;

  if (due) {
    const dayDelta = daysBetween(due, today);
    if (dayDelta < 0) {
      score += 180 + Math.min(90, Math.abs(dayDelta) * 12);
    } else {
      score += Math.max(0, 140 - dayDelta * 9);
    }
  } else {
    score += 25;
  }

  if (!scheduledStart) {
    score += 35;
  }

  if (typeof remainingHours === 'number' && Number.isFinite(remainingHours)) {
    score += Math.max(0, 45 - Math.min(45, remainingHours));
  }

  if (status === 'on_hold') {
    score -= 25;
  }

  return Math.round(Math.max(0, score));
};
