import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { PlusIcon, WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { DataTable, DataTableColumn } from '../ui/DataTable';
import { MobileDataCard } from '../ui/MobileDataCard';
import { StatusBadge } from '../ui/StatusBadge';
import { useToast } from '../ui/Toast';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { toDisplayString } from '../../utils/apiError';
import type { MaintenanceAsset, MaintenanceAssetInput } from '../../types/maintenance';

interface Props {
  /** Planning roles (ADMIN / MANAGER / SUPERVISOR) may add, edit and retire assets. */
  canEdit: boolean;
  /** Called after any successful write so the page's target pickers re-read the assets. */
  onChanged: () => void;
}

type AssetForm = Required<{ [K in keyof MaintenanceAssetInput]: string }>;

const BLANK_FORM: AssetForm = {
  asset_tag: '',
  name: '',
  asset_type: '',
  manufacturer: '',
  model: '',
  serial_number: '',
  location: '',
  notes: '',
};

const formFor = (asset: MaintenanceAsset): AssetForm => ({
  asset_tag: asset.asset_tag,
  name: asset.name,
  asset_type: asset.asset_type ?? '',
  manufacturer: asset.manufacturer ?? '',
  model: asset.model ?? '',
  serial_number: asset.serial_number ?? '',
  location: asset.location ?? '',
  notes: asset.notes ?? '',
});

const ACTIVE_COLORS = { active: 'bg-green-500/20 text-emerald-300', retired: 'bg-slate-800/50 text-slate-400' };

/**
 * Equipment assets: non-production machines (EZ-TRAKs, a manual Bridgeport, the shear,
 * forklifts, air compressors) that are maintained but never scheduled, dispatched, run on a
 * kiosk or counted in OEE. Retiring is `is_active = false` — the asset's schedules, work
 * orders and history stay. Every write is server-gated (tag unique per company ignoring
 * case, version CAS), so the list reflects only what the server returned.
 */
export function MaintenanceAssetsPanel({ canEdit, onChanged }: Props) {
  const { showToast } = useToast();
  const [assets, setAssets] = useState<MaintenanceAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [includeRetired, setIncludeRetired] = useState(false);
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search, 250);
  const [editing, setEditing] = useState<MaintenanceAsset | 'new' | null>(null);
  const [form, setForm] = useState<AssetForm>(BLANK_FORM);
  const [initialForm, setInitialForm] = useState<AssetForm>(BLANK_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');
  const [retireTarget, setRetireTarget] = useState<MaintenanceAsset | null>(null);
  const [retiring, setRetiring] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const rows = await api.getMaintenanceAssets({ include_inactive: includeRetired });
      setAssets(Array.isArray(rows) ? rows : []);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [includeRetired]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = editing !== null && JSON.stringify(form) !== JSON.stringify(initialForm);
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);

  const filtered = useMemo(() => {
    const needle = debouncedSearch.trim().toLowerCase();
    if (!needle) return assets;
    return assets.filter(asset =>
      [asset.asset_tag, asset.name, asset.asset_type, asset.location, asset.manufacturer, asset.model]
        .filter(Boolean)
        .some(value => String(value).toLowerCase().includes(needle))
    );
  }, [assets, debouncedSearch]);

  const openCreate = () => {
    setEditing('new');
    setForm(BLANK_FORM);
    setInitialForm(BLANK_FORM);
    setFormError('');
  };
  const openEdit = (asset: MaintenanceAsset) => {
    setEditing(asset);
    setForm(formFor(asset));
    setInitialForm(formFor(asset));
    setFormError('');
  };
  const closeForm = () => {
    if (saving) return;
    if (confirmDiscard()) setEditing(null);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || !editing) return;
    setSaving(true);
    setFormError('');
    const clean = (value: string) => value.trim() || null;
    try {
      if (editing === 'new') {
        await api.createMaintenanceAsset({
          asset_tag: form.asset_tag.trim(),
          name: form.name.trim(),
          asset_type: clean(form.asset_type),
          manufacturer: clean(form.manufacturer),
          model: clean(form.model),
          serial_number: clean(form.serial_number),
          location: clean(form.location),
          notes: clean(form.notes),
        });
        showToast('success', `Asset ${form.asset_tag.trim()} added`);
      } else {
        // Only the fields that changed are sent; `version` is the CAS (409 if stale).
        const changes: Record<string, string | null> = {};
        (Object.keys(form) as Array<keyof AssetForm>).forEach(key => {
          if (form[key] !== initialForm[key]) {
            changes[key] = key === 'asset_tag' || key === 'name' ? form[key].trim() : clean(form[key]);
          }
        });
        await api.updateMaintenanceAsset(editing.id, { version: editing.version, ...changes });
        showToast('success', `Asset ${form.asset_tag.trim()} updated`);
      }
      markSaved();
      setEditing(null);
      void load();
      onChanged();
    } catch (err: any) {
      setFormError(toDisplayString(err?.response?.data?.detail) || 'Unable to save the asset.');
    } finally {
      setSaving(false);
    }
  };

  const retire = async () => {
    if (!retireTarget || retiring) return;
    setRetiring(true);
    try {
      await api.retireMaintenanceAsset(retireTarget.id);
      showToast('success', `Asset ${retireTarget.asset_tag} retired`);
      setRetireTarget(null);
      void load();
      onChanged();
    } catch (err: any) {
      showToast('error', toDisplayString(err?.response?.data?.detail) || 'Unable to retire the asset.');
    } finally {
      setRetiring(false);
    }
  };

  const reactivate = async (asset: MaintenanceAsset) => {
    try {
      await api.updateMaintenanceAsset(asset.id, { version: asset.version, is_active: true });
      showToast('success', `Asset ${asset.asset_tag} returned to service`);
      void load();
      onChanged();
    } catch (err: any) {
      showToast('error', toDisplayString(err?.response?.data?.detail) || 'Unable to reactivate the asset.');
    }
  };

  const actions = (asset: MaintenanceAsset) =>
    canEdit ? (
      <div className="flex justify-end gap-2" role="presentation" onClick={e => e.stopPropagation()}>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => openEdit(asset)}
          aria-label={`Edit asset ${asset.asset_tag}`}
        >
          Edit
        </Button>
        {asset.is_active ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setRetireTarget(asset)}
            aria-label={`Retire asset ${asset.asset_tag}`}
          >
            Retire
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => reactivate(asset)}
            aria-label={`Reactivate asset ${asset.asset_tag}`}
          >
            Reactivate
          </Button>
        )}
      </div>
    ) : null;

  const columns: Array<DataTableColumn<MaintenanceAsset>> = [
    {
      key: 'asset_tag',
      header: 'Tag',
      sortable: true,
      accessor: asset => asset.asset_tag,
      render: asset => <span className="font-mono text-werco-navy-300">{asset.asset_tag}</span>,
    },
    { key: 'name', header: 'Name', sortable: true, className: 'font-medium', accessor: asset => asset.name },
    { key: 'asset_type', header: 'Type', sortable: true, accessor: asset => asset.asset_type ?? '' },
    {
      key: 'make',
      header: 'Make / Model',
      accessor: asset => [asset.manufacturer, asset.model].filter(Boolean).join(' '),
    },
    { key: 'location', header: 'Location', sortable: true, accessor: asset => asset.location ?? '' },
    {
      key: 'active_schedule_count',
      header: 'PM Schedules',
      sortable: true,
      align: 'right',
      accessor: asset => asset.active_schedule_count,
    },
    {
      key: 'open_work_order_count',
      header: 'Open WOs',
      sortable: true,
      align: 'right',
      accessor: asset => asset.open_work_order_count,
    },
    {
      key: 'is_active',
      header: 'Status',
      sortable: true,
      accessor: asset => (asset.is_active ? 'active' : 'retired'),
      render: asset => <StatusBadge status={asset.is_active ? 'active' : 'retired'} colorMap={ACTIVE_COLORS} />,
    },
    ...(canEdit ? [{ key: 'actions', header: 'Actions', align: 'right' as const, render: actions }] : []),
  ];

  const textField = (key: keyof AssetForm, label: string, required = false, maxLength = 100) => (
    <FormField label={label} required={required}>
      {field => (
        <input
          {...field}
          type="text"
          className="input"
          required={required}
          maxLength={maxLength}
          value={form[key]}
          onChange={e => setForm(prev => ({ ...prev, [key]: e.target.value }))}
        />
      )}
    </FormField>
  );

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-400">
        Equipment that is maintained but is not a production work center — it never appears in scheduling, dispatch, the
        kiosk or OEE, and its work orders never record machine downtime.
      </p>
      <DataTable<MaintenanceAsset>
        columns={columns}
        data={filtered}
        rowKey={asset => asset.id}
        rowClassName={asset => (asset.is_active ? '' : 'opacity-70')}
        defaultSort={{ key: 'asset_tag', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: 'maintenance-assets' }}
        loading={loading}
        error={loadError}
        onRetry={load}
        toolbarStart={
          <div className="flex flex-wrap items-center gap-3">
            <input
              type="text"
              aria-label="Search assets"
              placeholder="Search tag, name, type…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="input w-56"
            />
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={includeRetired} onChange={e => setIncludeRetired(e.target.checked)} />
              Show retired
            </label>
            {canEdit && (
              <Button size="sm" onClick={openCreate} className="inline-flex items-center">
                <PlusIcon className="mr-1 h-4 w-4" aria-hidden="true" />
                New Asset
              </Button>
            )}
          </div>
        }
        mobileCards={asset => (
          <MobileDataCard
            title={`${asset.asset_tag} · ${asset.name}`}
            subtitle={asset.asset_type || undefined}
            badge={<StatusBadge status={asset.is_active ? 'active' : 'retired'} colorMap={ACTIVE_COLORS} />}
            fields={[
              { label: 'Location', value: asset.location || '—' },
              { label: 'PM schedules', value: asset.active_schedule_count },
              { label: 'Open WOs', value: asset.open_work_order_count },
            ]}
            actions={actions(asset) ?? undefined}
          />
        )}
        empty={{
          icon: WrenchScrewdriverIcon,
          title: 'No equipment assets',
          description: 'Add forklifts, compressors and other non-production equipment to schedule their maintenance.',
          action: canEdit ? { label: 'New Asset', onClick: openCreate } : undefined,
        }}
      />

      <Modal
        open={editing !== null}
        onClose={closeForm}
        size="lg"
        closeOnBackdrop={false}
        closeOnEscape={!saving}
        ariaLabel="Equipment asset"
      >
        <form onSubmit={save} className="space-y-3">
          <h2 className="text-lg font-semibold text-white">
            {editing === 'new' ? 'New Equipment Asset' : 'Edit Equipment Asset'}
          </h2>
          {formError && (
            <p
              role="alert"
              className="rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200"
            >
              {formError}
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            {textField('asset_tag', 'Asset Tag', true, 50)}
            {textField('name', 'Name', true, 255)}
            {textField('asset_type', 'Type', false, 50)}
            {textField('location', 'Location')}
            {textField('manufacturer', 'Manufacturer')}
            {textField('model', 'Model')}
            {textField('serial_number', 'Serial Number')}
          </div>
          <FormField label="Notes">
            {field => (
              <textarea
                {...field}
                rows={2}
                maxLength={4000}
                className="input"
                value={form.notes}
                onChange={e => setForm(prev => ({ ...prev, notes: e.target.value }))}
              />
            )}
          </FormField>
          <div className="flex justify-end gap-2 border-t border-slate-700 pt-3">
            <Button variant="secondary" onClick={closeForm} disabled={saving}>
              Cancel
            </Button>
            <LoadingButton
              type="submit"
              loading={saving}
              loadingText="Saving…"
              disabled={!form.asset_tag.trim() || !form.name.trim()}
            >
              {editing === 'new' ? 'Add Asset' : 'Save Asset'}
            </LoadingButton>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={retireTarget !== null}
        title="Retire equipment asset"
        message={
          retireTarget
            ? `Retire ${retireTarget.asset_tag} (${retireTarget.name})? It leaves the target pickers; its ${retireTarget.active_schedule_count} active schedule(s), work orders and history are kept — deactivate its schedules separately if they should stop coming due.`
            : ''
        }
        confirmLabel="Retire"
        variant="warning"
        pending={retiring}
        onConfirm={retire}
        onCancel={() => setRetireTarget(null)}
      />
    </div>
  );
}

export default MaintenanceAssetsPanel;
