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

const parseDate = (value?: string | null) => {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed;
};

const startOfToday = () => {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
};

const daysBetween = (target: Date, base: Date) => {
  const oneDay = 24 * 60 * 60 * 1000;
  return Math.floor((target.getTime() - base.getTime()) / oneDay);
};

export const calculateDispatchScore = ({
  priority,
  dueDate,
  remainingHours,
  scheduledStart,
  status,
}: DispatchScoreInput): number => {
  const normalizedPriority = clampPriority(priority);
  const today = startOfToday();
  const due = parseDate(dueDate);

  let score = (11 - normalizedPriority) * 16;

  if (due) {
    const dayDelta = daysBetween(new Date(due.getFullYear(), due.getMonth(), due.getDate()), today);
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

