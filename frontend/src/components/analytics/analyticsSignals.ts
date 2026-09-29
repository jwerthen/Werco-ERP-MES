/** Presentation threshold: a measured KPI below 75% of its positive target needs attention. */
export function isSevereTargetMiss(value: number | null | undefined, target: number | null | undefined): boolean {
  return value != null && target != null && Number.isFinite(value) && Number.isFinite(target)
    && target > 0 && value < target * 0.75;
}

/** Flag adjacent reported-day jumps of at least 100 units and at least 5× the prior count. */
export function findProductionSpikes(points: Array<{ date: string; units_produced: number }>) {
  return points.flatMap((point, index) => {
    const previous = points[index - 1];
    if (!previous || !Number.isFinite(point.units_produced) || !Number.isFinite(previous.units_produced)) return [];
    return point.units_produced - previous.units_produced >= 100
      && point.units_produced >= Math.max(1, previous.units_produced) * 5
      ? [{ date: point.date, previous: previous.units_produced, value: point.units_produced }]
      : [];
  });
}
