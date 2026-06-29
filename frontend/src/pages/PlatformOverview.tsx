import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { BuildingOffice2Icon, UsersIcon, ClipboardDocumentListIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import { useCompany } from '../context/CompanyContext';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import { Modal } from '../components/ui/Modal';
import { EmptyState, ErrorState, useToast } from '../components/ui';

interface CompanyOverview {
  id: number;
  name: string;
  slug: string;
  logo_url?: string;
  active_users: number;
  active_work_orders: number;
}

interface OverviewData {
  total_companies: number;
  total_active_users: number;
  total_active_work_orders: number;
  companies: CompanyOverview[];
}

export default function PlatformOverview() {
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const { switchCompany } = useCompany();
  const navigate = useNavigate();
  const { showToast } = useToast();

  useEffect(() => {
    loadOverview();
  }, []);

  const loadOverview = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const data = await api.getPlatformOverview();
      setOverview(data);
    } catch {
      console.error('Failed to load platform overview');
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  const handleViewCompany = async (companyId: number) => {
    try {
      await switchCompany(companyId);
      navigate('/');
      window.location.reload();
    } catch {
      console.error('Failed to switch to company');
      showToast('error', 'Failed to switch to company');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <span className="loading loading-spinner loading-lg"></span>
      </div>
    );
  }

  return (
    <div className="p-4 max-w-7xl mx-auto space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-bold text-base-content truncate">Platform Overview</h1>
          <p className="text-xs text-fd-mute mt-0.5">Monitor all companies across the platform</p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="btn btn-primary btn-sm flex-shrink-0"
        >
          Add Company
        </button>
      </div>

      {loadError ? (
        <ErrorState
          message="Could not load the platform overview."
          onRetry={loadOverview}
        />
      ) : (
        <>
      {/* Summary Stats */}
      <MiniStatStrip className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        <MiniStat
          icon={BuildingOffice2Icon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Total Companies"
          value={overview?.total_companies || 0}
          valueColor="text-fd-blue"
        />
        <MiniStat
          icon={UsersIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Total Active Users"
          value={overview?.total_active_users || 0}
          valueColor="text-fd-green"
        />
        <MiniStat
          icon={ClipboardDocumentListIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Active Work Orders"
          value={overview?.total_active_work_orders || 0}
          valueColor="text-fd-cyan"
        />
      </MiniStatStrip>

      {/* Companies */}
      <CockpitPanel title="Companies" subtitle={`${overview?.companies.length || 0} total`}>
        {overview && overview.companies.length === 0 ? (
          <EmptyState
            icon={BuildingOffice2Icon}
            title="No companies yet"
            description="Companies you create will appear here."
            action={{ label: 'Add Company', onClick: () => setShowCreateModal(true) }}
          />
        ) : (
        <div className="divide-y divide-fd-line">
          {overview?.companies.map((company) => (
            <button
              key={company.id}
              type="button"
              onClick={() => handleViewCompany(company.id)}
              className="w-full flex items-center gap-3 px-1 py-2 text-left transition-colors hover:bg-fd-raised min-w-0"
            >
              {company.logo_url ? (
                <img src={company.logo_url} alt="" className="h-8 w-8 rounded-sm object-contain bg-fd-raised flex-shrink-0" />
              ) : (
                <div className="h-8 w-8 rounded-sm bg-fd-blue/20 flex items-center justify-center flex-shrink-0">
                  <span className="text-fd-blue font-bold text-sm">{company.name.charAt(0)}</span>
                </div>
              )}
              <div className="min-w-0 flex-1">
                <p className="font-semibold text-sm text-base-content truncate">{company.name}</p>
                <p className="text-[11px] text-fd-faint truncate">{company.slug}</p>
              </div>
              <span className="text-xs text-fd-mute tabular-nums whitespace-nowrap flex-shrink-0">
                {company.active_users} users &middot; {company.active_work_orders} WOs
              </span>
            </button>
          ))}
        </div>
        )}
      </CockpitPanel>
        </>
      )}

      {/* Create Company Modal */}
      <CreateCompanyModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onCreated={() => {
          setShowCreateModal(false);
          loadOverview();
        }}
      />
    </div>
  );
}


function CreateCompanyModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [form, setForm] = useState({
    name: '',
    admin_email: '',
    admin_first_name: '',
    admin_last_name: '',
    admin_password: '',
  });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  // The shared <Modal> keeps this component mounted while closed, so reset the
  // form (clearing the typed admin password) and error whenever it closes —
  // otherwise the next open would prefill the previous credentials/error.
  useEffect(() => {
    if (!open) {
      setForm({ name: '', admin_email: '', admin_first_name: '', admin_last_name: '', admin_password: '' });
      setError('');
      setLoading(false);
    }
  }, [open]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await api.createCompany(form);
      onCreated();
    } catch (e: any) {
      setError(e.response?.data?.detail || 'Failed to create company');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} size="md" ariaLabelledBy="create-company-title">
      <h3 id="create-company-title" className="font-bold text-lg mb-4">Add New Company</h3>
      <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className="label text-sm">Company Name</label>
            <input
              type="text"
              className="input input-bordered input-sm w-full"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label text-sm">Admin First Name</label>
              <input
                type="text"
                className="input input-bordered input-sm w-full"
                value={form.admin_first_name}
                onChange={(e) => setForm({ ...form, admin_first_name: e.target.value })}
                required
              />
            </div>
            <div>
              <label className="label text-sm">Admin Last Name</label>
              <input
                type="text"
                className="input input-bordered input-sm w-full"
                value={form.admin_last_name}
                onChange={(e) => setForm({ ...form, admin_last_name: e.target.value })}
                required
              />
            </div>
          </div>
          <div>
            <label className="label text-sm">Admin Email</label>
            <input
              type="email"
              className="input input-bordered input-sm w-full"
              value={form.admin_email}
              onChange={(e) => setForm({ ...form, admin_email: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="label text-sm">Admin Password</label>
            <input
              type="password"
              className="input input-bordered input-sm w-full"
              value={form.admin_password}
              onChange={(e) => setForm({ ...form, admin_password: e.target.value })}
              required
              minLength={12}
            />
            <p className="text-xs text-base-content/50 mt-1">Min 12 chars, uppercase, lowercase, number, special char</p>
          </div>

          {error && <div className="text-error text-sm">{error}</div>}

          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary btn-sm" disabled={loading}>
              {loading ? <span className="loading loading-spinner loading-xs"></span> : 'Create Company'}
            </button>
          </div>
      </form>
    </Modal>
  );
}
