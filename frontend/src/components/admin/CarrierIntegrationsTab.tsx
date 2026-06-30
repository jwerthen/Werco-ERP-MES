/**
 * Admin > Carriers / Integrations tab.
 *
 * Per-company admin console for the multi-carrier shipping integration:
 *   - manage aggregator credentials (EasyPost / Zenkraft) -- keys are WRITE-ONLY;
 *     the read shape only ever exposes ``api_key_last4``, never a full key;
 *   - run a credential-only Test Connection (transmits no customer data);
 *   - edit the company shipping profile (ship-from origin + default package dims)
 *     and the ``allow_carrier_egress`` kill switch, which gates ALL outbound
 *     carrier calls that transmit customer addresses and defaults OFF until a
 *     human signs off on CUI / data-egress.
 *
 * This tab is rendered inside AdminSettings, which is already route-gated on the
 * ``admin:settings`` permission, so all routes here are admin-only.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  XMarkIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  TruckIcon,
  ArrowPathIcon,
  SignalIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useToast } from '../ui/Toast';
import { LoadingButton } from '../ui/LoadingButton';
import { SelectField } from '../ui/SelectField';
import type {
  CarrierAccount,
  CarrierAccountCreate,
  CarrierAccountUpdate,
  CompanyShippingProfile,
  CompanyShippingProfileUpdate,
} from '../../types/shipping';

const PROVIDER_OPTIONS = [
  { value: 'easypost', label: 'EasyPost', description: 'Parcel rate-shop, labels, address validation, tracking' },
  { value: 'zenkraft', label: 'Zenkraft', description: 'Freight / LTL + native FedEx Freight (BOL)' },
];

const ENVIRONMENT_OPTIONS = [
  { value: 'production', label: 'Production' },
  { value: 'test', label: 'Test / Sandbox' },
];

// The bring-your-own-carrier account refs the UI exposes as discrete fields.
const CARRIER_REF_FIELDS: { key: string; label: string; placeholder: string }[] = [
  { key: 'fedex', label: 'FedEx account ref', placeholder: 'EasyPost carrier-account id for FedEx' },
  { key: 'ups', label: 'UPS account ref', placeholder: 'EasyPost carrier-account id for UPS' },
  { key: 'fedex_freight', label: 'FedEx Freight account ref', placeholder: 'Carrier-account id for FedEx Freight (LTL)' },
];

const errorDetail = (err: any, fallback: string): string =>
  err?.response?.data?.detail || err?.message || fallback;

export default function CarrierIntegrationsTab() {
  const { showToast } = useToast();

  const [accounts, setAccounts] = useState<CarrierAccount[]>([]);
  const [profile, setProfile] = useState<CompanyShippingProfile | null>(null);
  const [loading, setLoading] = useState(true);

  const [accountModalOpen, setAccountModalOpen] = useState(false);
  const [editingAccount, setEditingAccount] = useState<CarrierAccount | null>(null);
  const [deletingAccount, setDeletingAccount] = useState<CarrierAccount | null>(null);
  const [testingId, setTestingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const accountList = await api.getCarrierAccounts(true);
      setAccounts(accountList);
    } catch (err) {
      showToast('error', errorDetail(err, 'Failed to load carrier accounts'));
    }
    // The shipping profile 404s until first configured -- treat that as "none yet".
    try {
      const prof = await api.getShippingProfile();
      setProfile(prof);
    } catch (err: any) {
      if (err?.response?.status === 404) {
        setProfile(null);
      } else {
        showToast('error', errorDetail(err, 'Failed to load shipping profile'));
      }
    }
    setLoading(false);
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  const handleSaveAccount = async (data: CarrierAccountCreate | CarrierAccountUpdate, id?: number) => {
    if (id != null) {
      await api.updateCarrierAccount(id, data as CarrierAccountUpdate);
      showToast('success', 'Carrier account updated');
    } else {
      await api.createCarrierAccount(data as CarrierAccountCreate);
      showToast('success', 'Carrier account created');
    }
    setAccountModalOpen(false);
    setEditingAccount(null);
    await load();
  };

  const handleDeleteAccount = async () => {
    if (!deletingAccount) return;
    try {
      await api.deleteCarrierAccount(deletingAccount.id);
      showToast('success', `Carrier account "${deletingAccount.name}" deleted`);
      setDeletingAccount(null);
      await load();
    } catch (err) {
      showToast('error', errorDetail(err, 'Failed to delete carrier account'));
    }
  };

  const handleTestConnection = async (account: CarrierAccount) => {
    setTestingId(account.id);
    try {
      const result = await api.testCarrierConnection(account.id);
      if (result.ok) {
        showToast('success', result.message || `${account.provider} connection OK`);
      } else {
        showToast('error', result.message || `${account.provider} connection failed`);
      }
    } catch (err) {
      showToast('error', errorDetail(err, 'Connection test failed'));
    } finally {
      setTestingId(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Egress status banner */}
      <EgressBanner enabled={!!profile?.allow_carrier_egress} configured={!!profile} />

      {/* Carrier accounts */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
              <TruckIcon className="h-5 w-5 text-werco-600" />
              Carrier Accounts
            </h3>
            <p className="text-xs text-surface-500 mt-0.5">
              Aggregator credentials. API keys are encrypted at rest and never displayed in full.
            </p>
          </div>
          <button
            onClick={() => { setEditingAccount(null); setAccountModalOpen(true); }}
            className="btn-primary btn-sm"
          >
            <PlusIcon className="h-4 w-4 mr-1" />
            Add Account
          </button>
        </div>

        {accounts.length === 0 ? (
          <div className="text-center py-10 border border-surface-200 rounded">
            <TruckIcon className="h-10 w-10 text-surface-300 mx-auto mb-2" />
            <p className="text-surface-500 text-sm">No carrier accounts configured yet</p>
          </div>
        ) : (
          <div className="table-container border border-surface-200">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Provider</th>
                  <th>Environment</th>
                  <th>API Key</th>
                  <th>Carrier Refs</th>
                  <th>Status</th>
                  <th className="w-32 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <tr key={account.id} className={account.is_active ? '' : 'opacity-50'}>
                    <td className="font-medium">
                      {account.name}
                      {account.is_default && (
                        <span className="badge badge-info ml-2">Default</span>
                      )}
                    </td>
                    <td className="uppercase text-xs font-mono">{account.provider}</td>
                    <td className="text-sm text-surface-600">{account.environment || '—'}</td>
                    <td className="font-mono text-sm">
                      {account.api_key_last4 ? `••••${account.api_key_last4}` : '••••'}
                    </td>
                    <td className="text-xs text-surface-600">
                      {account.carrier_refs.length > 0 ? account.carrier_refs.join(', ') : '—'}
                    </td>
                    <td>
                      {account.is_active ? (
                        <span className="badge badge-success">Active</span>
                      ) : (
                        <span className="badge badge-neutral">Inactive</span>
                      )}
                    </td>
                    <td>
                      <div className="flex items-center justify-end gap-1">
                        <button
                          onClick={() => handleTestConnection(account)}
                          disabled={testingId === account.id}
                          className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-500/10 disabled:opacity-50"
                          title="Test connection"
                        >
                          {testingId === account.id ? (
                            <ArrowPathIcon className="h-4 w-4 animate-spin" />
                          ) : (
                            <SignalIcon className="h-4 w-4" />
                          )}
                        </button>
                        <button
                          onClick={() => { setEditingAccount(account); setAccountModalOpen(true); }}
                          className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-500/10"
                          title="Edit"
                        >
                          <PencilIcon className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => setDeletingAccount(account)}
                          className="p-2 rounded-lg text-surface-500 hover:text-red-600 hover:bg-red-500/10"
                          title="Delete"
                        >
                          <TrashIcon className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Company shipping profile */}
      <ShippingProfileForm
        profile={profile}
        onSaved={(saved) => { setProfile(saved); showToast('success', 'Shipping profile saved'); }}
        onError={(msg) => showToast('error', msg)}
      />

      {accountModalOpen && (
        <CarrierAccountModal
          account={editingAccount}
          onSave={handleSaveAccount}
          onClose={() => { setAccountModalOpen(false); setEditingAccount(null); }}
          onError={(msg) => showToast('error', msg)}
        />
      )}

      {deletingAccount && (
        <div
          className="modal-overlay"
          role="presentation"
          onClick={(e) => { if (e.target === e.currentTarget) setDeletingAccount(null); }}
        >
          <div className="modal max-w-md">
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Delete carrier account</h3>
              <button onClick={() => setDeletingAccount(null)} className="p-2 rounded-lg hover:bg-surface-100">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <div className="p-5">
              <p className="text-sm text-surface-700">
                Delete carrier account <span className="font-semibold">{deletingAccount.name}</span>? It is
                soft-deleted, so purchased labels referencing it stay intact, but it can no longer be used for
                new shipments.
              </p>
            </div>
            <div className="modal-footer flex justify-end gap-2 p-5 pt-0">
              <button onClick={() => setDeletingAccount(null)} className="btn-secondary">Cancel</button>
              <button onClick={handleDeleteAccount} className="btn bg-red-600 text-white hover:bg-red-700 px-4 py-2">
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Egress status banner.
// ---------------------------------------------------------------------------

function EgressBanner({ enabled, configured }: { enabled: boolean; configured: boolean }) {
  if (enabled) {
    return (
      <div className="flex items-start gap-3 rounded border border-green-500/40 bg-green-500/5 px-4 py-3">
        <CheckCircleIcon className="h-5 w-5 text-green-500 flex-shrink-0 mt-0.5" />
        <div className="text-sm">
          <p className="font-semibold text-green-400">Carrier egress is ENABLED</p>
          <p className="text-surface-500 mt-0.5">
            Rate-shop, address validation, label/BOL purchase, and pickups are live. Customer ship-to
            addresses are transmitted to the configured carrier aggregator. Disable below if egress is no
            longer authorized.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-3 rounded border border-amber-500/40 bg-amber-500/5 px-4 py-3">
      <ExclamationTriangleIcon className="h-5 w-5 text-amber-500 flex-shrink-0 mt-0.5" />
      <div className="text-sm">
        <p className="font-semibold text-amber-400">Carrier egress is DISABLED</p>
        <p className="text-surface-500 mt-0.5">
          {configured
            ? 'Rate-shop, address validation, and label purchase will be rejected until egress is enabled below.'
            : 'No shipping profile is configured yet. Carrier features are disabled until you save a profile and enable egress below.'}{' '}
          Enabling egress transmits customer addresses to a third-party carrier/aggregator and requires
          CUI / data-egress sign-off first.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Carrier account create/edit modal.
// ---------------------------------------------------------------------------

interface AccountFormState {
  name: string;
  provider: string;
  environment: string;
  api_key: string;
  webhook_secret: string;
  carrier_refs: Record<string, string>;
  is_active: boolean;
  is_default: boolean;
}

function CarrierAccountModal({
  account,
  onSave,
  onClose,
  onError,
}: {
  account: CarrierAccount | null;
  onSave: (data: CarrierAccountCreate | CarrierAccountUpdate, id?: number) => Promise<void>;
  onClose: () => void;
  onError: (msg: string) => void;
}) {
  const isEdit = account != null;
  const [form, setForm] = useState<AccountFormState>({
    name: account?.name || '',
    provider: account?.provider || 'easypost',
    environment: account?.environment || 'production',
    api_key: '',
    webhook_secret: '',
    // Existing carrier-ref VALUES are never returned (only the keys); start the
    // discrete fields empty and only send keys the admin (re)enters.
    carrier_refs: {},
    is_active: account?.is_active ?? true,
    is_default: account?.is_default ?? false,
  });
  const [saving, setSaving] = useState(false);

  const update = <K extends keyof AccountFormState>(key: K, value: AccountFormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const updateRef = (key: string, value: string) =>
    setForm((prev) => ({ ...prev, carrier_refs: { ...prev.carrier_refs, [key]: value } }));

  const existingRefKeys = useMemo(() => account?.carrier_refs || [], [account]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) {
      onError('Account name is required');
      return;
    }
    if (!isEdit && !form.api_key.trim()) {
      onError('API key is required');
      return;
    }

    // Only include carrier-ref entries the admin actually filled in.
    const carrierRefs: Record<string, string> = {};
    Object.entries(form.carrier_refs).forEach(([k, v]) => {
      if (v.trim()) carrierRefs[k] = v.trim();
    });

    setSaving(true);
    try {
      if (isEdit) {
        const payload: CarrierAccountUpdate = {
          name: form.name.trim(),
          environment: form.environment,
          is_active: form.is_active,
          is_default: form.is_default,
        };
        // Secrets rotate ONLY when re-entered; omit otherwise to keep the stored value.
        if (form.api_key.trim()) payload.api_key = form.api_key.trim();
        if (form.webhook_secret.trim()) payload.webhook_secret = form.webhook_secret.trim();
        if (Object.keys(carrierRefs).length > 0) payload.carrier_refs = carrierRefs;
        await onSave(payload, account!.id);
      } else {
        const payload: CarrierAccountCreate = {
          name: form.name.trim(),
          provider: form.provider,
          environment: form.environment,
          api_key: form.api_key.trim(),
          carrier_refs: carrierRefs,
          is_active: form.is_active,
          is_default: form.is_default,
        };
        if (form.webhook_secret.trim()) payload.webhook_secret = form.webhook_secret.trim();
        await onSave(payload);
      }
    } catch (err) {
      onError(errorDetail(err, 'Failed to save carrier account'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="modal-overlay"
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="modal max-w-2xl">
        <div className="modal-header">
          <h3 className="text-lg font-semibold">{isEdit ? 'Edit Carrier Account' : 'Add Carrier Account'}</h3>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-surface-100">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4 max-h-[70vh] overflow-y-auto">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="label">Name</label>
              <input
                className="input"
                value={form.name}
                onChange={(e) => update('name', e.target.value)}
                placeholder="e.g. EasyPost (production)"
                required
              />
            </div>
            <div>
              <label className="label">Provider</label>
              <SelectField
                value={form.provider}
                options={PROVIDER_OPTIONS}
                onChange={(v) => update('provider', String(v))}
                disabled={isEdit}
                ariaLabel="Provider"
              />
              {isEdit && (
                <p className="text-xs text-surface-500 mt-1">Provider can&apos;t be changed after creation.</p>
              )}
            </div>
            <div>
              <label className="label">Environment</label>
              <SelectField
                value={form.environment}
                options={ENVIRONMENT_OPTIONS}
                onChange={(v) => update('environment', String(v))}
                ariaLabel="Environment"
              />
            </div>
            <div>
              <label className="label">
                API Key {isEdit && <span className="text-surface-500 font-normal">(leave blank to keep)</span>}
              </label>
              <input
                type="password"
                autoComplete="new-password"
                className="input font-mono"
                value={form.api_key}
                onChange={(e) => update('api_key', e.target.value)}
                placeholder={isEdit && account?.api_key_last4 ? `••••${account.api_key_last4}` : 'Write-only; encrypted at rest'}
              />
            </div>
          </div>

          <div>
            <label className="label">
              Webhook Secret <span className="text-surface-500 font-normal">(optional, write-only)</span>
            </label>
            <input
              type="password"
              autoComplete="new-password"
              className="input font-mono"
              value={form.webhook_secret}
              onChange={(e) => update('webhook_secret', e.target.value)}
              placeholder={isEdit && account?.has_webhook_secret ? 'Configured — leave blank to keep' : 'Used to verify inbound tracking webhooks'}
            />
          </div>

          <div>
            <label className="label">Bring-your-own carrier account refs</label>
            <p className="text-xs text-surface-500 mb-2">
              Optional per-carrier account identifiers from your aggregator. Values are not displayed after
              saving; re-enter to change.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {CARRIER_REF_FIELDS.map((field) => (
                <div key={field.key}>
                  <label className="text-xs text-surface-600">
                    {field.label}
                    {existingRefKeys.includes(field.key) && (
                      <span className="badge badge-neutral ml-1">set</span>
                    )}
                  </label>
                  <input
                    className="input font-mono"
                    value={form.carrier_refs[field.key] || ''}
                    onChange={(e) => updateRef(field.key, e.target.value)}
                    placeholder={field.placeholder}
                  />
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-6 pt-2">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                className="checkbox"
                checked={form.is_active}
                onChange={(e) => update('is_active', e.target.checked)}
              />
              <span className="text-sm text-surface-700">Active</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                className="checkbox"
                checked={form.is_default}
                onChange={(e) => update('is_default', e.target.checked)}
              />
              <span className="text-sm text-surface-700">Default account</span>
            </label>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <LoadingButton type="submit" loading={saving} loadingText="Saving…">
              {isEdit ? 'Save Changes' : 'Create Account'}
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Company shipping profile form (ship-from + egress kill switch).
// ---------------------------------------------------------------------------

const emptyProfileForm: CompanyShippingProfileUpdate = {
  ship_from_name: '',
  ship_from_company: '',
  ship_from_phone: '',
  ship_from_email: '',
  ship_from_street1: '',
  ship_from_street2: '',
  ship_from_city: '',
  ship_from_state: '',
  ship_from_zip: '',
  ship_from_country: 'US',
  default_package_weight_lbs: '',
  default_package_length_in: '',
  default_package_width_in: '',
  default_package_height_in: '',
  allow_carrier_egress: false,
};

function ShippingProfileForm({
  profile,
  onSaved,
  onError,
}: {
  profile: CompanyShippingProfile | null;
  onSaved: (saved: CompanyShippingProfile) => void;
  onError: (msg: string) => void;
}) {
  const buildForm = useCallback(
    (p: CompanyShippingProfile | null): CompanyShippingProfileUpdate => {
      if (!p) return { ...emptyProfileForm };
      return {
        ship_from_name: p.ship_from_name ?? '',
        ship_from_company: p.ship_from_company ?? '',
        ship_from_phone: p.ship_from_phone ?? '',
        ship_from_email: p.ship_from_email ?? '',
        ship_from_street1: p.ship_from_street1 ?? '',
        ship_from_street2: p.ship_from_street2 ?? '',
        ship_from_city: p.ship_from_city ?? '',
        ship_from_state: p.ship_from_state ?? '',
        ship_from_zip: p.ship_from_zip ?? '',
        ship_from_country: p.ship_from_country ?? 'US',
        default_package_weight_lbs: p.default_package_weight_lbs ?? '',
        default_package_length_in: p.default_package_length_in ?? '',
        default_package_width_in: p.default_package_width_in ?? '',
        default_package_height_in: p.default_package_height_in ?? '',
        allow_carrier_egress: p.allow_carrier_egress,
      };
    },
    [],
  );

  const [form, setForm] = useState<CompanyShippingProfileUpdate>(() => buildForm(profile));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(buildForm(profile));
  }, [profile, buildForm]);

  const update = <K extends keyof CompanyShippingProfileUpdate>(key: K, value: CompanyShippingProfileUpdate[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  // Normalize the numeric package-dimension fields: send numbers or null, never "".
  const toNumOrNull = (v: number | string | null | undefined): number | null => {
    if (v === '' || v == null) return null;
    const n = typeof v === 'number' ? v : parseFloat(v);
    return Number.isFinite(n) ? n : null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      const payload: CompanyShippingProfileUpdate = {
        ...form,
        default_package_weight_lbs: toNumOrNull(form.default_package_weight_lbs),
        default_package_length_in: toNumOrNull(form.default_package_length_in),
        default_package_width_in: toNumOrNull(form.default_package_width_in),
        default_package_height_in: toNumOrNull(form.default_package_height_in),
      };
      const saved = await api.updateShippingProfile(payload);
      onSaved(saved);
    } catch (err) {
      onError(errorDetail(err, 'Failed to save shipping profile'));
    } finally {
      setSaving(false);
    }
  };

  const egressOn = !!form.allow_carrier_egress;

  return (
    <section>
      <div className="mb-3">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700">Company Shipping Profile</h3>
        <p className="text-xs text-surface-500 mt-0.5">
          The ship-from origin used on labels and the carrier egress kill switch.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="label">Ship-from name</label>
            <input className="input" value={form.ship_from_name ?? ''} onChange={(e) => update('ship_from_name', e.target.value)} />
          </div>
          <div>
            <label className="label">Company</label>
            <input className="input" value={form.ship_from_company ?? ''} onChange={(e) => update('ship_from_company', e.target.value)} />
          </div>
          <div>
            <label className="label">Phone</label>
            <input className="input" value={form.ship_from_phone ?? ''} onChange={(e) => update('ship_from_phone', e.target.value)} />
          </div>
          <div>
            <label className="label">Email</label>
            <input type="email" className="input" value={form.ship_from_email ?? ''} onChange={(e) => update('ship_from_email', e.target.value)} />
          </div>
          <div className="md:col-span-2">
            <label className="label">Street address</label>
            <input className="input" value={form.ship_from_street1 ?? ''} onChange={(e) => update('ship_from_street1', e.target.value)} placeholder="Street 1" />
          </div>
          <div className="md:col-span-2">
            <input className="input" value={form.ship_from_street2 ?? ''} onChange={(e) => update('ship_from_street2', e.target.value)} placeholder="Street 2 (optional)" />
          </div>
          <div>
            <label className="label">City</label>
            <input className="input" value={form.ship_from_city ?? ''} onChange={(e) => update('ship_from_city', e.target.value)} />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="label">State</label>
              <input className="input" value={form.ship_from_state ?? ''} onChange={(e) => update('ship_from_state', e.target.value)} />
            </div>
            <div>
              <label className="label">ZIP</label>
              <input className="input" value={form.ship_from_zip ?? ''} onChange={(e) => update('ship_from_zip', e.target.value)} />
            </div>
            <div>
              <label className="label">Country</label>
              <input className="input" value={form.ship_from_country ?? ''} onChange={(e) => update('ship_from_country', e.target.value)} placeholder="US" />
            </div>
          </div>
        </div>

        <div>
          <label className="label">Default package dimensions</label>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <label className="text-xs text-surface-600">Weight (lbs)</label>
              <input type="number" step="0.01" min="0" className="input" value={form.default_package_weight_lbs ?? ''} onChange={(e) => update('default_package_weight_lbs', e.target.value)} />
            </div>
            <div>
              <label className="text-xs text-surface-600">Length (in)</label>
              <input type="number" step="0.01" min="0" className="input" value={form.default_package_length_in ?? ''} onChange={(e) => update('default_package_length_in', e.target.value)} />
            </div>
            <div>
              <label className="text-xs text-surface-600">Width (in)</label>
              <input type="number" step="0.01" min="0" className="input" value={form.default_package_width_in ?? ''} onChange={(e) => update('default_package_width_in', e.target.value)} />
            </div>
            <div>
              <label className="text-xs text-surface-600">Height (in)</label>
              <input type="number" step="0.01" min="0" className="input" value={form.default_package_height_in ?? ''} onChange={(e) => update('default_package_height_in', e.target.value)} />
            </div>
          </div>
        </div>

        {/* Egress kill switch */}
        <div className={`rounded border px-4 py-4 ${egressOn ? 'border-red-500/40 bg-red-500/5' : 'border-surface-200'}`}>
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              className="checkbox mt-0.5"
              checked={egressOn}
              onChange={(e) => update('allow_carrier_egress', e.target.checked)}
            />
            <span>
              <span className="flex items-center gap-2 text-sm font-semibold text-surface-800">
                <ExclamationTriangleIcon className="h-4 w-4 text-amber-500" />
                Allow carrier egress
              </span>
              <span className="block text-xs text-surface-500 mt-1">
                Enabling this transmits customer ship-to addresses and identity to a third-party carrier /
                aggregator (e.g. EasyPost) for rate-shop, address validation, and label/BOL purchase. These
                addresses may be CUI under some DoD contracts — obtain CUI / data-egress sign-off before
                enabling. Off by default; toggling it is recorded on the tamper-evident audit trail.
              </span>
            </span>
          </label>
        </div>

        <div className="flex justify-end">
          <LoadingButton type="submit" loading={saving} loadingText="Saving…">
            Save Shipping Profile
          </LoadingButton>
        </div>
      </form>
    </section>
  );
}
