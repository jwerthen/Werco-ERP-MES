import React, { useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import type { WorkingCalendar } from '../../types/workingCalendar';

const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
interface Props {
  center: { id: number; code: string; name: string };
  onClose: () => void;
  onSaved: () => void;
}

export default function WorkingCalendarEditor({ center, onClose, onSaved }: Props) {
  const [calendar, setCalendar] = useState<WorkingCalendar | null>(null);
  const [original, setOriginal] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [stale, setStale] = useState(false);
  const pending = useRef(false);
  const dirty = calendar !== null && JSON.stringify(calendar) !== original;
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);
  const close = () => {
    if (!pending.current && confirmDiscard()) onClose();
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    api
      .getWorkingCalendar(center.id)
      .then(value => {
        if (!active) return;
        setCalendar(value);
        setOriginal(JSON.stringify(value));
        setStale(false);
      })
      .catch(() => {
        if (active) setError('Could not load the working calendar. Retry to edit it.');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [center.id, reload]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!calendar || pending.current || stale) return;
    const dates = calendar.overrides.map(row => row.date);
    if (new Set(dates).size !== dates.length) {
      setError('Use one override per date.');
      return;
    }
    pending.current = true;
    setSaving(true);
    setError('');
    try {
      const saved = await api.updateWorkingCalendar(center.id, {
        expected_version: calendar.version,
        weekly_hours: calendar.weekly_hours,
        overrides: calendar.overrides,
      });
      setCalendar(saved);
      setOriginal(JSON.stringify(saved));
      markSaved();
      onSaved();
      onClose();
    } catch (err) {
      const status = (err as { response?: { status?: number } }).response?.status;
      setStale(status === 409);
      setError(
        status === 409
          ? 'The calendar changed while you were editing. Your entries remain below. Reload the current calendar before saving a new version.'
          : 'Calendar save was not confirmed. Your entries remain below. Retry to save; a version conflict requires reloading.'
      );
    } finally {
      pending.current = false;
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      onClose={close}
      ariaLabel={`Working calendar — ${center.code}`}
      size="2xl"
      closeOnBackdrop={!saving}
      closeOnEscape={!saving}
    >
      <form onSubmit={save} className="space-y-4">
        <div>
          <h2 className="text-xl font-semibold text-white">Working calendar — {center.code}</h2>
          <p className="text-sm text-fd-muted">{center.name}. Dates use the plant calendar (America/Chicago).</p>
        </div>
        {error && (
          <div role="alert" className="rounded border border-amber-500/40 p-3 text-amber-200">
            {error}
          </div>
        )}
        {(stale || (!calendar && !loading)) && (
          <Button
            variant="secondary"
            onClick={() => {
              if (confirmDiscard()) setReload(value => value + 1);
            }}
          >
            Reload current calendar
          </Button>
        )}
        {loading ? (
          <p role="status">Loading working calendar…</p>
        ) : (
          calendar && (
            <>
              <p className="text-sm text-fd-muted">
                Enter the combined available hours across all shifts each day. Zero closes that day. Saved calendars
                affect new scheduling and capacity checks; existing jobs keep their dates and may show overload.
              </p>
              {calendar.version === 0 && (
                <p className="text-sm text-amber-200">
                  No working calendar is configured. These hours reflect the existing daily capacity. Saving enables
                  calendar rules for this center.
                </p>
              )}
              <Button
                variant="secondary"
                size="sm"
                disabled={saving}
                onClick={() => setCalendar({ ...calendar, weekly_hours: [8, 8, 8, 8, 8, 0, 0] })}
              >
                Use Monday–Friday, 8 hours
              </Button>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {weekdays.map((day, index) => (
                  <label key={day} className="text-sm">
                    {day}
                    <input
                      className="input mt-1 w-full"
                      type="number"
                      min="0"
                      max="24"
                      step="0.25"
                      required
                      disabled={saving}
                      value={calendar.weekly_hours[index]}
                      onChange={event =>
                        setCalendar({
                          ...calendar,
                          weekly_hours: calendar.weekly_hours.map((hours, i) =>
                            i === index ? Number(event.target.value) : hours
                          ),
                        })
                      }
                    />
                  </label>
                ))}
              </div>
              <div className="flex items-center justify-between gap-2">
                <h3 className="font-semibold">Holidays and date overrides</h3>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={saving || calendar.overrides.length >= 400}
                  onClick={() =>
                    setCalendar({ ...calendar, overrides: [...calendar.overrides, { date: '', hours: 0, reason: '' }] })
                  }
                >
                  Add date override
                </Button>
              </div>
              {calendar.overrides.length === 0 && (
                <p className="text-sm text-fd-muted">
                  No date overrides. Add a holiday, shutdown, overtime day, or shortened shift.
                </p>
              )}
              {calendar.overrides.map((row, index) => (
                <div key={index} className="grid grid-cols-2 gap-2 rounded border border-fd-border p-3 sm:grid-cols-4">
                  <label className="text-sm">
                    Date
                    <input
                      aria-label={`Override ${index + 1} date`}
                      type="date"
                      className="input mt-1 w-full"
                      required
                      disabled={saving}
                      value={row.date}
                      onChange={event =>
                        setCalendar({
                          ...calendar,
                          overrides: calendar.overrides.map((item, i) =>
                            i === index ? { ...item, date: event.target.value } : item
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="text-sm">
                    Hours (0 = closed)
                    <input
                      aria-label={`Override ${index + 1} hours`}
                      type="number"
                      min="0"
                      max="24"
                      step="0.25"
                      className="input mt-1 w-full"
                      required
                      disabled={saving}
                      value={row.hours}
                      onChange={event =>
                        setCalendar({
                          ...calendar,
                          overrides: calendar.overrides.map((item, i) =>
                            i === index ? { ...item, hours: Number(event.target.value) } : item
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="text-sm">
                    Reason
                    <input
                      aria-label={`Override ${index + 1} reason`}
                      className="input mt-1 w-full"
                      required
                      maxLength={200}
                      disabled={saving}
                      value={row.reason}
                      onChange={event =>
                        setCalendar({
                          ...calendar,
                          overrides: calendar.overrides.map((item, i) =>
                            i === index ? { ...item, reason: event.target.value } : item
                          ),
                        })
                      }
                    />
                  </label>
                  <Button
                    variant="ghost"
                    disabled={saving}
                    onClick={() =>
                      setCalendar({ ...calendar, overrides: calendar.overrides.filter((_, i) => i !== index) })
                    }
                  >
                    Remove override {index + 1}
                  </Button>
                </div>
              ))}
            </>
          )
        )}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" disabled={saving} onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={loading || saving || stale || !calendar || (!dirty && calendar.version > 0)}>
            {saving ? 'Saving calendar…' : 'Save working calendar'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
