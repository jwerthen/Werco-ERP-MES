import React, { useCallback, useEffect, useState } from 'react';
import api from '../../services/api';
import type { EmailRecipientsSettings } from '../../types/emailRecipients';
import { ErrorState, useToast } from '../ui';

export default function EmailRecipientsTab() {
  const { showToast } = useToast();
  const [data, setData] = useState<EmailRecipientsSettings | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [eventKey, setEventKey] = useState('wo.completed');
  const [drafts, setDrafts] = useState<Record<string, number[]>>({});
  const [search, setSearch] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      setData(await api.getEmailRecipients());
    } catch {
      setLoadError(true);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const save = async (ids: number[] | null) => {
    setSaving(true);
    setSaveError('');
    try {
      setData(await api.updateEmailRecipients(eventKey, ids));
      setDrafts(previous => {
        const next = { ...previous };
        delete next[eventKey];
        return next;
      });
      showToast('success', ids === null ? 'Default email recipients restored.' : 'Email recipients saved.');
    } catch {
      setSaveError('Could not save email recipients. Your selections are still here; please try again.');
    } finally {
      setSaving(false);
    }
  };

  if (loadError) return <ErrorState message="Could not load email recipients." onRetry={load} />;
  if (!data)
    return (
      <p role="status" className="py-8 text-surface-500">
        Loading email recipients…
      </p>
    );

  const event = data.events.find(item => item.event_key === eventKey);
  if (!event) return <p>No configurable email notifications are available.</p>;
  const selected = drafts[eventKey] ?? event.user_ids;
  const dirty = drafts[eventKey] !== undefined;
  const query = search.trim().toLowerCase();
  const visibleUsers = data.users.filter(
    user =>
      (user.is_active || selected?.includes(user.id)) && `${user.name} ${user.email}`.toLowerCase().includes(query)
  );
  const selectedNames = (selected ?? []).map(
    id => data.users.find(user => user.id === id)?.name ?? `Unavailable user #${id}`
  );

  const toggle = (id: number, checked: boolean) => {
    const ids = selected ?? [];
    setDrafts(previous => ({ ...previous, [eventKey]: checked ? [...ids, id] : ids.filter(value => value !== id) }));
  };

  return (
    <section className="space-y-5" aria-labelledby="email-recipients-heading">
      <div>
        <h2 id="email-recipients-heading" className="text-lg font-semibold text-surface-900">
          Email Recipients
        </h2>
        <p className="mt-1 text-sm text-surface-600">
          Choose who receives each automated email for this company. Saved selections control email delivery, including
          the recipients’ own actions. In-app alerts and SMS keep their existing settings.
        </p>
      </div>

      <div className="max-w-xl">
        <label htmlFor="email-notification-type" className="block text-sm font-medium text-surface-700 mb-1">
          Email type
        </label>
        <select
          id="email-notification-type"
          className="input w-full"
          value={eventKey}
          disabled={saving}
          onChange={e => {
            setEventKey(e.target.value);
            setSearch('');
            setSaveError('');
          }}
        >
          {Array.from(new Set(data.events.map(item => item.category))).map(category => (
            <optgroup key={category} label={category}>
              {data.events
                .filter(item => item.category === category)
                .map(item => (
                  <option key={item.event_key} value={item.event_key}>
                    {item.label}
                    {drafts[item.event_key] ? ' (unsaved)' : ''}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
      </div>

      <div className="rounded-lg border border-surface-200 p-4 space-y-4">
        <div>
          <h3 className="font-semibold text-surface-900">{event.label}</h3>
          <p className="text-sm text-surface-500 mt-1">{event.description}</p>
        </div>
        {event.missing_default_emails.length > 0 && !event.is_custom && (
          <p role="status" className="text-sm text-amber-700">
            These default addresses have no active user account in this company:{' '}
            {event.missing_default_emails.join(', ')}. Update the user accounts to enable delivery.
          </p>
        )}
        {selected === null ? (
          <div className="space-y-3">
            <p className="text-sm text-surface-600">
              Recipients are currently chosen automatically by role, department, or record ownership.
            </p>
            <button className="btn-secondary" onClick={() => setDrafts(previous => ({ ...previous, [eventKey]: [] }))}>
              Choose specific recipients
            </button>
          </div>
        ) : (
          <>
            <p className="text-sm text-surface-700" aria-live="polite">
              {selected.length
                ? `${selected.length} selected: ${selectedNames.join(', ')}`
                : 'Nobody selected. Saving this list turns off this email.'}
            </p>
            <label className="block">
              <span className="block text-sm font-medium text-surface-700 mb-1">Find a recipient</span>
              <input
                type="search"
                className="input w-full max-w-xl"
                placeholder="Search by name or email"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </label>
            <fieldset disabled={saving} className="grid grid-cols-1 md:grid-cols-2 gap-2 max-h-96 overflow-y-auto">
              <legend className="sr-only">Recipients for {event.label}</legend>
              {visibleUsers.map(user => {
                const checked = selected.includes(user.id);
                const unavailable = !user.is_active || !user.email_deliverable;
                return (
                  <label key={user.id} className="flex items-start gap-3 rounded-lg border border-surface-200 p-3">
                    <input
                      type="checkbox"
                      className="checkbox mt-1"
                      checked={checked}
                      disabled={unavailable && !checked}
                      aria-label={`${user.name} (${user.email})`}
                      onChange={e => toggle(user.id, e.target.checked)}
                    />
                    <span className="min-w-0 text-sm">
                      <span className="block font-medium text-surface-800">{user.name}</span>
                      <span className="block text-surface-500 break-all">{user.email}</span>
                      {unavailable && (
                        <span className="block text-amber-700">
                          {!user.is_active
                            ? 'Inactive account — no email delivery'
                            : 'Needs a deliverable email address'}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </fieldset>
            {visibleUsers.length === 0 && <p className="text-sm text-surface-500">No matching recipients.</p>}
            {(selected ?? [])
              .filter(id => !data.users.some(user => user.id === id))
              .map(id => (
                <button key={id} className="btn-secondary" disabled={saving} onClick={() => toggle(id, false)}>
                  Remove unavailable user #{id}
                </button>
              ))}
          </>
        )}
        {saveError && (
          <p role="alert" className="text-sm text-red-600">
            {saveError}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3 border-t border-surface-200 pt-4">
          <button className="btn-primary" disabled={saving || !dirty} onClick={() => void save(selected)}>
            {saving ? 'Saving…' : 'Save recipients'}
          </button>
          {event.is_custom && (
            <button className="btn-secondary" disabled={saving} onClick={() => void save(null)}>
              Restore default recipients
            </button>
          )}
          {dirty && <span className="text-sm text-amber-700">Unsaved changes for this email type</span>}
        </div>
      </div>
      <p className="text-xs text-surface-500">
        Account security emails and manually sent documents use their own recipients.
      </p>
    </section>
  );
}
