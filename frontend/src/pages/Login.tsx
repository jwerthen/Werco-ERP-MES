import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ShieldCheckIcon, LockClosedIcon } from '@heroicons/react/24/outline';

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await login(email, password);
      navigate('/');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Login failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex">
      {/* Left side - Branding */}
      <div className="hidden lg:flex lg:w-1/2 bg-gradient-to-br from-werco-600 via-werco-700 to-werco-900 p-12 flex-col justify-between">
        <div>
          <img 
            src="/Werco_Logo-PNG.png" 
            alt="Werco Manufacturing" 
            className="h-16 brightness-0 invert" 
          />
        </div>
        
        <div className="space-y-6">
          <h1 className="text-4xl font-bold text-white leading-tight">
            Manufacturing<br />Execution System
          </h1>
          <p className="text-lg text-white/70 max-w-md">
            Precision fabrication management for aerospace & defense manufacturing.
          </p>
          
          <div className="flex items-center gap-6 pt-4">
            <div className="flex items-center gap-2 text-white/80">
              <ShieldCheckIcon className="h-5 w-5" />
              <span className="text-sm font-medium">AS9100D Compliant</span>
            </div>
            <div className="flex items-center gap-2 text-white/80">
              <ShieldCheckIcon className="h-5 w-5" />
              <span className="text-sm font-medium">ISO 9001 Certified</span>
            </div>
          </div>
        </div>
        
        <p className="text-white/40 text-sm">
          &copy; {new Date().getFullYear()} Werco Manufacturing. All rights reserved.
        </p>
      </div>

      {/* Right side - Login form */}
      <div className="flex-1 flex items-center justify-center bg-surface-50 p-8">
        <div className="w-full max-w-md">
          {/* Mobile logo */}
          <div className="lg:hidden text-center mb-8">
            <img 
              src="/Werco_Logo-PNG.png" 
              alt="Werco Manufacturing" 
              className="h-16 mx-auto mb-4" 
            />
          </div>

          <div className="bg-white rounded-2xl shadow-xl p-8 border border-surface-200">
            <div className="text-center mb-8">
              <div className="w-14 h-14 bg-werco-100 rounded-2xl flex items-center justify-center mx-auto mb-4">
                <LockClosedIcon className="h-7 w-7 text-werco-600" />
              </div>
              <h2 className="text-2xl font-bold text-surface-900">Welcome back</h2>
              <p className="text-surface-500 mt-1">Sign in to your account</p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-5">
              {error && (
                <div className="alert-danger">
                  <span className="text-sm">{error}</span>
                </div>
              )}

              <div>
                <label htmlFor="email" className="label">Email Address</label>
                <input
                  id="email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="input"
                  placeholder="you@werco.com"
                  autoComplete="email"
                />
              </div>

              <div>
                <label htmlFor="password" className="label">Password</label>
                <input
                  id="password"
                  type="password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="input"
                  placeholder="Enter your password"
                  autoComplete="current-password"
                />
              </div>

              <button
                type="submit"
                disabled={loading}
                className="btn-primary w-full py-3 text-base"
              >
                {loading ? (
                  <span className="flex items-center justify-center gap-2">
                    <div className="spinner h-5 w-5 border-white/30 border-t-white"></div>
                    Signing in...
                  </span>
                ) : (
                  'Sign In'
                )}
              </button>
            </form>
          </div>

          {/* Mobile compliance badges */}
          <div className="lg:hidden flex items-center justify-center gap-4 mt-6 text-surface-500">
            <div className="flex items-center gap-1.5 text-xs">
              <ShieldCheckIcon className="h-4 w-4" />
              <span>AS9100D</span>
            </div>
            <div className="flex items-center gap-1.5 text-xs">
              <ShieldCheckIcon className="h-4 w-4" />
              <span>ISO 9001</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
