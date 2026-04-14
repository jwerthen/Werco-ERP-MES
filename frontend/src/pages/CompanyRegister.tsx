import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import api from '../services/api';
import {
  ShieldCheckIcon,
  BuildingOffice2Icon,
} from '@heroicons/react/24/outline';

export default function CompanyRegister() {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    company_name: '',
    admin_email: '',
    admin_first_name: '',
    admin_last_name: '',
    admin_password: '',
    confirm_password: '',
  });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (form.admin_password !== form.confirm_password) {
      setError('Passwords do not match');
      return;
    }

    setLoading(true);
    try {
      await api.registerCompany({
        company_name: form.company_name,
        admin_email: form.admin_email,
        admin_first_name: form.admin_first_name,
        admin_last_name: form.admin_last_name,
        admin_password: form.admin_password,
      });
      setSuccess(true);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      if (Array.isArray(detail)) {
        setError(detail.map((d: any) => d.msg || d).join('; '));
      } else {
        setError(detail || 'Registration failed. Please try again.');
      }
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-[#0d1117] via-[#151b28] to-[#1a1f2e] p-8">
        <div className="bg-[rgba(21,27,40,0.95)] backdrop-blur-xl border border-slate-700/50 rounded-3xl shadow-xl p-10 max-w-md w-full text-center">
          <div className="w-16 h-16 mx-auto mb-6 rounded-2xl flex items-center justify-center bg-gradient-to-br from-green-500/20 to-green-600/20">
            <ShieldCheckIcon className="h-8 w-8 text-green-400" />
          </div>
          <h2 className="text-2xl font-bold text-slate-100 mb-3">Company Created</h2>
          <p className="text-slate-400 mb-8">
            Your company has been set up. You can now sign in with your admin credentials.
          </p>
          <button
            onClick={() => navigate('/login')}
            className="du-btn du-btn-primary du-btn-block h-14 normal-case text-base font-semibold"
          >
            Go to Sign In
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-[#0d1117] via-[#151b28] to-[#1a1f2e] p-8">
      <div className="w-full max-w-md">
        <div className="bg-[rgba(21,27,40,0.95)] backdrop-blur-xl border border-slate-700/50 rounded-3xl shadow-xl p-10 relative overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-[#1B4D9C] via-[#3366FF] to-[#1B4D9C]" />

          <div className="text-center mb-8">
            <div className="w-16 h-16 mx-auto mb-5 rounded-2xl flex items-center justify-center shadow-lg bg-gradient-to-br from-[#0a1628] to-[#0f2952]">
              <BuildingOffice2Icon className="h-8 w-8 text-blue-300" />
            </div>
            <h2 className="text-2xl font-bold text-slate-100">Register Your Company</h2>
            <p className="text-slate-400 mt-2">Set up your company on the Werco ERP platform</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-5">
            {error && (
              <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-3 text-red-400 text-sm">
                {error}
              </div>
            )}

            <div className="space-y-2">
              <label className="block text-sm font-medium text-slate-300">Company Name</label>
              <input
                type="text"
                required
                value={form.company_name}
                onChange={(e) => setForm({ ...form, company_name: e.target.value })}
                className="du-input du-input-bordered w-full h-12 bg-[#151b28]"
                placeholder="Your Company LLC"
              />
            </div>

            <div className="border-t border-slate-700/30 pt-4">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-3">Admin Account</p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="block text-sm font-medium text-slate-300">First Name</label>
                <input
                  type="text"
                  required
                  value={form.admin_first_name}
                  onChange={(e) => setForm({ ...form, admin_first_name: e.target.value })}
                  className="du-input du-input-bordered w-full h-12 bg-[#151b28]"
                />
              </div>
              <div className="space-y-2">
                <label className="block text-sm font-medium text-slate-300">Last Name</label>
                <input
                  type="text"
                  required
                  value={form.admin_last_name}
                  onChange={(e) => setForm({ ...form, admin_last_name: e.target.value })}
                  className="du-input du-input-bordered w-full h-12 bg-[#151b28]"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="block text-sm font-medium text-slate-300">Email</label>
              <input
                type="email"
                required
                value={form.admin_email}
                onChange={(e) => setForm({ ...form, admin_email: e.target.value })}
                className="du-input du-input-bordered w-full h-12 bg-[#151b28]"
                placeholder="admin@yourcompany.com"
              />
            </div>

            <div className="space-y-2">
              <label className="block text-sm font-medium text-slate-300">Password</label>
              <input
                type="password"
                required
                minLength={12}
                value={form.admin_password}
                onChange={(e) => setForm({ ...form, admin_password: e.target.value })}
                className="du-input du-input-bordered w-full h-12 bg-[#151b28]"
              />
              <p className="text-xs text-slate-400">Min 12 chars, uppercase, lowercase, number, special char</p>
            </div>

            <div className="space-y-2">
              <label className="block text-sm font-medium text-slate-300">Confirm Password</label>
              <input
                type="password"
                required
                value={form.confirm_password}
                onChange={(e) => setForm({ ...form, confirm_password: e.target.value })}
                className="du-input du-input-bordered w-full h-12 bg-[#151b28]"
              />
            </div>

            <button
              type="submit"
              disabled={loading}
              className="du-btn du-btn-primary du-btn-block h-14 normal-case text-base font-semibold"
            >
              {loading ? (
                <span className="du-loading du-loading-spinner du-loading-sm" />
              ) : (
                'Create Company'
              )}
            </button>
          </form>

          <div className="mt-8 pt-6 border-t border-slate-700/30">
            <p className="text-center text-sm text-slate-400">
              Already have an account?{' '}
              <Link to="/login" className="du-link du-link-primary font-semibold">Sign in</Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
