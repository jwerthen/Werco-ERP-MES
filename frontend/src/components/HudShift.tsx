import React, { useEffect, useRef, useState } from 'react';
import api from '../services/api';
import type { ProductionCalendar } from '../types/productionCalendar';
import { currentDefaultShift, productionDayEnded } from '../utils/productionCalendar';
import { formatCentralTime } from '../utils/centralTime';

/** Re-evaluate the badge this often so it flips at shift boundaries. */
const TICK_MS = 30_000;
/** Pick up an Admin's calendar edit without a reload. */
const REFRESH_MS = 30 * 60_000;
/** After the production day ends, ask for the next one at most this often. */
const ROLLOVER_RETRY_MS = 60_000;
/** After a failed read, wait this long before asking again. */
const FAILURE_RETRY_MS = 5 * 60_000;

const timeRange = (startUtc: string, endUtc: string) => `${formatCentralTime(startUtc)} – ${formatCentralTime(endUtc)}`;

/**
 * The header HUD's SHIFT badge: the code of the shift running now in the company's
 * DEFAULT shift pattern, read from `GET /production-calendar`.
 *
 * The server computes today's shift occurrences as UTC instants on the company clock,
 * so the badge only compares them with the current time -- no zone or day-start math
 * here. In a hand-off overlap it shows the OUTGOING shift (the server's rule). It
 * refetches when the production day ends and every half hour; any failure, an
 * unconfigured calendar, or a gap between shifts reads as an em dash. It never blocks
 * the layout: nothing waits on the request.
 */
const HudShift = React.memo(function HudShift() {
  const [calendar, setCalendar] = useState<ProductionCalendar | null>(null);
  const [failed, setFailed] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const calendarRef = useRef<ProductionCalendar | null>(null);
  const lastAttempt = useRef(0);
  const lastFailed = useRef(false);
  const inFlight = useRef(false);
  // Whether the component is mounted NOW -- not whether the effect run that started a
  // request is still current. Under StrictMode (development) the effect mounts, unmounts
  // and mounts again; the second run skips its fetch because the first is in flight, so
  // the first run's answer must still be applied, or the badge stays blank until the
  // next refetch.
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    const fetchCalendar = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      lastAttempt.current = Date.now();
      try {
        const data = await api.getProductionCalendar();
        calendarRef.current = data;
        lastFailed.current = false;
        if (!mounted.current) return;
        setCalendar(data);
        setFailed(false);
      } catch {
        // Degrade to the em dash; the last good calendar stays until its day ends.
        lastFailed.current = true;
        if (mounted.current) setFailed(true);
      } finally {
        inFlight.current = false;
      }
    };
    const tick = () => {
      const t = Date.now();
      setNow(t);
      const since = t - lastAttempt.current;
      const retryAfter = lastFailed.current ? FAILURE_RETRY_MS : ROLLOVER_RETRY_MS;
      if ((productionDayEnded(calendarRef.current, t) && since >= retryAfter) || since >= REFRESH_MS) {
        void fetchCalendar();
      }
    };
    void fetchCalendar();
    const id = setInterval(tick, TICK_MS);
    return () => {
      mounted.current = false;
      clearInterval(id);
    };
  }, []);

  let code = '—';
  let title: string;
  if (!calendar || productionDayEnded(calendar, now)) {
    title = failed ? 'Shift unavailable' : 'Loading shift…';
  } else if (!calendar.configured) {
    title = 'No production calendar configured';
  } else {
    const { current, overlapping } = currentDefaultShift(calendar, now);
    if (current) {
      code = current.code;
      const handOff = overlapping.filter(occ => occ.shift_id !== current.shift_id).map(occ => occ.code);
      title = `${current.name} · ${timeRange(current.start_utc, current.end_utc)}`;
      if (handOff.length > 0) title += ` · hand-off with ${handOff.join(', ')}`;
    } else {
      title = 'No active shift';
    }
  }

  return (
    <span className="text-fd-ink" title={title} data-testid="hud-shift">
      {code}
    </span>
  );
});

export default HudShift;
