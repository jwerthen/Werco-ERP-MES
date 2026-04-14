import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import { useCompany } from '../context/CompanyContext';

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
  const [showCreateModal, setShowCreateModal] = useState(false);
  const { switchCompany } = useCompany();
  const navigate = useNavigate();

  useEffect(() => {
    loadOverview();
  }, []);

  const loadOverview = async () => {
    try {
      const data = await api.getPlatformOverview();
      setOverview(data);
    } catch (e) {
      console.error('Failed to load platform overview');
    } finally {
      setLoading(false);
    }
  };

  const handleViewCompany = async (companyId: number) => {
    try {
      await switchCompany(companyId);
      navigate('/');
      window.location.reload();
    } catch (e) {
      console.error('Failed to switch to company');
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
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-base-content">Platform Overview</h1>
          <p className="text-sm text-base-content/60 mt-1">Monitor all companies across the platform</p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="btn btn-primary btn-sm"
        >
          Add Company
        </button>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <div className="bg-base-200 rounded-lg p-4 border border-base-300">
          <div className="text-sm text-base-content/60">Total Companies</div>
          <div className="text-3xl font-bold text-primary mt-1">{overview?.total_companies || 0}</div>
        </div>
        <div className="bg-base-200 rounded-lg p-4 border border-base-300">
          <div className="text-sm text-base-content/60">Total Active Users</div>
          <div className="text-3xl font-bold text-success mt-1">{overview?.total_active_users || 0}</div>
        </div>
        <div className="bg-base-200 rounded-lg p-4 border border-base-300">
          <div className="text-sm text-base-content/60">Active Work Orders</div>
          <div className="text-3xl font-bold text-info mt-1">{overview?.total_active_work_orders || 0}</div>
        </div>
      </div>

      {/* Company Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {overview?.companies.map((company) => (
          <div key={company.id} className="bg-base-200 rounded-lg border border-base-300 hover:border-primary/30 transition-colors">
            <div className="p-4">
              <div className="flex items-center gap-3 mb-3">
                {company.logo_url ? (
                  <img src={company.logo_url} alt="" className="h-10 w-10 rounded-lg object-contain bg-base-300" />
                ) : (
                  <div className="h-10 w-10 rounded-lg bg-primary/20 flex items-center justify-center">
                    <span className="text-primary font-bold text-lg">{company.name.charAt(0)}</span>
                  </div>
                )}
                <div>
                  <h3 className="font-semibold text-base-content">{company.name}</h3>
                  <p className="text-xs text-base-content/50">{company.slug}</p>
                </div>
              </div>

              <div className="flex items-center gap-4 text-sm text-base-content/60 mb-4">
                <div className="flex items-center gap-1">
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                    <path d="M9 6a3 3 0 11-6 0 3 3 0 016 0zM17 6a3 3 0 11-6 0 3 3 0 016 0zM12.93 17c.046-.327.07-.66.07-1a6.97 6.97 0 00-1.5-4.33A5 5 0 0119 16v1h-6.07zM6 11a5 5 0 015 5v1H1v-1a5 5 0 015-5z" />
                  </svg>
                  {company.active_users} users
                </div>
                <div className="flex items-center gap-1">
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M6 2a1 1 0 00-1 1v1H4a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-1V3a1 1 0 10-2 0v1H7V3a1 1 0 00-1-1zm0 5a1 1 0 000 2h8a1 1 0 100-2H6z" clipRule="evenodd" />
                  </svg>
                  {company.active_work_orders} active WOs
                </div>
              </div>

              <button
                onClick={() => handleViewCompany(company.id)}
                className="btn btn-outline btn-sm w-full"
              >
                View Company
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Create Company Modal */}
      {showCreateModal && (
        <CreateCompanyModal
          onClose={() => setShowCreateModal(false)}
          onCreated={() => {
            setShowCreateModal(false);
            loadOverview();
          }}
        />
      )}
    </div>
  );
}


function CreateCompanyModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({
    name: '',
    admin_email: '',
    admin_first_name: '',
    admin_last_name: '',
    admin_password: '',
  });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

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
    <div className="modal modal-open">
      <div className="modal-box bg-base-200">
        <h3 className="font-bold text-lg mb-4">Add New Company</h3>
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

          <div className="modal-action">
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary btn-sm" disabled={loading}>
              {loading ? <span className="loading loading-spinner loading-xs"></span> : 'Create Company'}
            </button>
          </div>
        </form>
      </div>
      <div className="modal-backdrop" onClick={onClose}></div>
    </div>
  );
}
