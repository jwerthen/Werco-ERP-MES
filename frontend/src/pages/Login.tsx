import React, { useState, useEffect, useMemo } from 'react';
import { useNavigate, useLocation, Link } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ShieldCheckIcon, LockClosedIcon, EnvelopeIcon, EyeIcon, EyeSlashIcon } from '@heroicons/react/24/outline';

// Blueprint grid pattern - matches wercomfg.com aesthetic
const BlueprintGrid = () => (
  <svg className="absolute inset-0 w-full h-full opacity-[0.04]" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <pattern id="blueprint-grid" width="40" height="40" patternUnits="userSpaceOnUse">
        <path
          d="M0 0h40v40H0z"
          fill="none"
          stroke="currentColor"
          strokeWidth="0.5"
          className="text-blue-300"
        />
        <path
          d="M20 0v40M0 20h40"
          fill="none"
          stroke="currentColor"
          strokeWidth="0.25"
          className="text-blue-300"
        />
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#blueprint-grid)" />
  </svg>
);

// Subtle animated background elements
const AnimatedBackground = () => (
  <div className="absolute inset-0 overflow-hidden pointer-events-none">
    {/* Soft gradient orbs */}
    <div className="absolute top-1/4 left-1/4 w-64 h-64 bg-blue-500/100/8 rounded-full blur-3xl animate-pulse" />
    <div
      className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-blue-600/6 rounded-full blur-3xl animate-pulse"
      style={{ animationDelay: '1s' }}
    />
    <div
      className="absolute top-1/2 left-1/2 w-72 h-72 bg-blue-400/4 rounded-full blur-3xl animate-pulse"
      style={{ animationDelay: '2s' }}
    />

    {/* Power flow lines - confined to upper portion to avoid overlapping stat cards */}
    {[...Array(3)].map((_, i) => (
      <div
        key={i}
        className="absolute h-px bg-gradient-to-r from-transparent via-blue-400/20 to-transparent"
        style={{
          width: '200%',
          top: `${15 + i * 12}%`,
          left: '-50%',
          animation: `flowLine ${10 + i * 3}s linear infinite`,
          animationDelay: `${i * 2}s`,
        }}
      />
    ))}
  </div>
);

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [employeeId, setEmployeeId] = useState('');
  const [loginMode, setLoginMode] = useState<'employee' | 'email'>('employee');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [focusedField, setFocusedField] = useState<string | null>(null);
  const { login, loginWithEmployeeId } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const kioskParam = useMemo(() => new URLSearchParams(location.search).get('kiosk'), [location.search]);
  const forceEmployeeMode = kioskParam === '1';

  useEffect(() => {
    if (forceEmployeeMode) {
      setLoginMode('employee');
    }
  }, [forceEmployeeMode]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (loginMode === 'employee') {
        const rawEmployeeId = employeeId.trim();
        if (rawEmployeeId.length === 0) {
          throw new Error('Employee ID required');
        }

        // Numeric-only input is treated as a badge ID and normalized to 4 digits.
        const digitsOnly = rawEmployeeId.replace(/\D/g, '');
        const employeeLoginId = /^\d+$/.test(rawEmployeeId) ? digitsOnly.slice(-4).padStart(4, '0') : rawEmployeeId;

        await loginWithEmployeeId(employeeLoginId);
        try {
          const storedUser = sessionStorage.getItem('user');
          const signedInUser = storedUser ? JSON.parse(storedUser) : null;
          if (signedInUser?.role === 'operator') {
            navigate('/shop-floor/operations?kiosk=1', { replace: true });
            return;
          }
        } catch {
          // Ignore parsing issues and fall back to default route below.
        }
      } else {
        await login(email, password);
      }
      navigate('/', { replace: true });
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Login failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex">
      {/* CSS for custom animations */}
      <style>{`
        @keyframes flowLine {
          0% { transform: translateX(-50%); }
          100% { transform: translateX(50%); }
        }
        .glass-card {
          background: rgba(21, 27, 40, 0.95);
          backdrop-filter: blur(20px);
          border: 1px solid rgba(51, 65, 85, 0.5);
        }
      `}</style>

      {/* Left side - Werco Branding Panel */}
      <div className="hidden lg:flex lg:w-1/2 relative overflow-hidden">
        {/* Deep navy background - matching wercomfg.com */}
        <div
          className="absolute inset-0"
          style={{
            background: 'linear-gradient(135deg, #0a1628 0%, #0f2952 40%, #123266 60%, #0a1628 100%)',
          }}
        />

        {/* Background patterns */}
        <BlueprintGrid />
        <AnimatedBackground />

        {/* Gradient overlays for depth */}
        <div className="absolute inset-0 bg-gradient-to-t from-[#0a1628]/80 via-transparent to-[#0a1628]/40" />

        {/* Content */}
        <div className="relative z-10 p-12 flex flex-col justify-between w-full">
          <div>
            <img
              src="/Werco_Logo-PNG.png"
              alt="Werco Manufacturing"
              className="h-14 brightness-0 invert drop-shadow-lg"
            />
          </div>

          <div className="space-y-8">
            {/* Main heading */}
            <div>
              <h1 className="text-5xl font-bold leading-tight">
                <span className="text-white">Manufacturing</span>
                <br />
                <span className="bg-gradient-to-r from-blue-300 via-blue-400 to-white bg-clip-text text-transparent">
                  Execution System
                </span>
              </h1>
            </div>

            {/* Tagline from wercomfg.com */}
            <p className="text-lg text-slate-300 max-w-md leading-relaxed">
              Built for What Flies, Fights, and Powers the Future.
            </p>

            {/* Key metrics - matching wercomfg.com */}
            <div className="grid grid-cols-3 gap-4 pt-2">
              <div className="text-center p-3 rounded-xl bg-white/[0.04] border border-white/[0.08]">
                <div className="text-2xl font-bold text-white">24hr</div>
                <div className="text-xs font-mono uppercase tracking-wider text-blue-300/60 mt-1">RFQ Response</div>
              </div>
              <div className="text-center p-3 rounded-xl bg-white/[0.04] border border-white/[0.08]">
                <div className="text-2xl font-bold text-white">95%+</div>
                <div className="text-xs font-mono uppercase tracking-wider text-blue-300/60 mt-1">On-Time</div>
              </div>
              <div className="text-center p-3 rounded-xl bg-white/[0.04] border border-white/[0.08]">
                <div className="text-2xl font-bold text-white">99%+</div>
                <div className="text-xs font-mono uppercase tracking-wider text-blue-300/60 mt-1">First-Pass</div>
              </div>
            </div>

            {/* Certification badges */}
            <div className="flex flex-wrap items-center gap-3 pt-4">
              <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/[0.04] border border-white/[0.08]">
                <ShieldCheckIcon className="h-4 w-4 text-blue-400" />
                <span className="text-sm font-medium text-white/90">AS9100D</span>
              </div>
              <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/[0.04] border border-white/[0.08]">
                <ShieldCheckIcon className="h-4 w-4 text-blue-400" />
                <span className="text-sm font-medium text-white/90">ISO 9001</span>
              </div>
              <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/[0.04] border border-white/[0.08]">
                <ShieldCheckIcon className="h-4 w-4 text-blue-400" />
                <span className="text-sm font-medium text-white/90">ITAR</span>
              </div>
            </div>
          </div>

          <p className="text-slate-500 text-sm">&copy; 2026 Werco Manufacturing. All rights reserved.</p>
        </div>
      </div>

      {/* Right side - Login form */}
      <div className="flex-1 flex items-center justify-center bg-gradient-to-br from-[#0d1117] via-[#151b28] to-[#1a1f2e] p-8 relative">
        {/* Subtle dot pattern */}
        <div className="absolute inset-0 opacity-[0.02]">
          <svg className="w-full h-full" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <pattern id="dots" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse">
                <circle cx="2" cy="2" r="1" className="fill-slate-900" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#dots)" />
          </svg>
        </div>

        <div className="w-full max-w-md relative z-10">
          {/* Mobile logo */}
          <div className="lg:hidden text-center mb-8">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-14 mx-auto mb-2" />
            <p className="text-slate-500 text-sm">Manufacturing Execution System</p>
          </div>

          {/* Login card */}
          <div className="glass-card rounded-3xl shadow-xl p-10 relative overflow-hidden">
            {/* Top accent bar - Werco blue gradient */}
            <div
              className="absolute top-0 left-0 right-0 h-1"
              style={{ background: 'linear-gradient(90deg, #1B4D9C 0%, #3366FF 50%, #1B4D9C 100%)' }}
            />

            <div className="text-center mb-8">
              {/* Lock icon container - Werco navy */}
              <div className="relative w-16 h-16 mx-auto mb-5">
                <div className="absolute inset-0 bg-gradient-to-br from-werco-navy-600 to-blue-700 rounded-2xl rotate-3 opacity-20" />
                <div className="absolute inset-0 bg-gradient-to-br from-werco-navy-600 to-blue-700 rounded-2xl -rotate-3 opacity-20" />
                <div
                  className="relative w-full h-full rounded-2xl flex items-center justify-center shadow-lg"
                  style={{ background: 'linear-gradient(135deg, #0a1628 0%, #0f2952 100%)' }}
                >
                  <LockClosedIcon className="h-8 w-8 text-blue-300" />
                </div>
              </div>
              <h2 className="text-2xl font-bold text-slate-100">Welcome back</h2>
              <p className="text-slate-500 mt-2">Sign in to access your dashboard</p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-6">
              <div
                className={`du-join du-join-horizontal grid grid-cols-2 w-full ${forceEmployeeMode ? 'opacity-70 pointer-events-none' : ''}`}
              >
                <button
                  type="button"
                  onClick={() => setLoginMode('employee')}
                  className={`du-btn du-join-item h-11 normal-case text-sm font-semibold ${
                    loginMode === 'employee' ? 'du-btn-primary' : 'du-btn-outline'
                  }`}
                >
                  Employee ID
                </button>
                <button
                  type="button"
                  onClick={() => setLoginMode('email')}
                  className={`du-btn du-join-item h-11 normal-case text-sm font-semibold ${
                    loginMode === 'email' ? 'du-btn-primary' : 'du-btn-outline'
                  }`}
                >
                  Email Login
                </button>
              </div>

              {error && (
                <div className="du-alert du-alert-error animate-fade-in">
                  <svg className="w-5 h-5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path
                      fillRule="evenodd"
                      d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z"
                      clipRule="evenodd"
                    />
                  </svg>
                  <span>{error}</span>
                </div>
              )}

              {loginMode === 'employee' ? (
                <div className="space-y-2">
                  <label htmlFor="employeeId" className="block text-sm font-medium text-slate-300">
                    Employee ID
                  </label>
                  <div className="relative">
                    <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                      <LockClosedIcon
                        className={`h-5 w-5 transition-colors duration-200 ${
                          focusedField === 'employeeId' ? 'text-werco-navy-600' : 'text-slate-400'
                        }`}
                      />
                    </div>
                    <input
                      id="employeeId"
                      type="text"
                      inputMode="text"
                      maxLength={50}
                      required
                      value={employeeId}
                      onChange={e => setEmployeeId(e.target.value.replace(/[^A-Za-z0-9\-_]/g, '').slice(0, 50))}
                      onFocus={() => setFocusedField('employeeId')}
                      onBlur={() => setFocusedField(null)}
                      className="du-input du-input-bordered w-full h-14 pl-12 pr-4 text-center text-lg bg-[#151b28]"
                      placeholder="0000 or EMP-1001"
                      autoComplete="off"
                    />
                  </div>
                  <p className="text-xs text-slate-500">Use your employee ID or 4-digit badge ID.</p>
                </div>
              ) : (
                <>
                  {/* Email field */}
                  <div className="space-y-2">
                    <label htmlFor="email" className="block text-sm font-medium text-slate-300">
                      Email Address
                    </label>
                    <div className="relative">
                      <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                        <EnvelopeIcon
                          className={`h-5 w-5 transition-colors duration-200 ${
                            focusedField === 'email' ? 'text-werco-navy-600' : 'text-slate-400'
                          }`}
                        />
                      </div>
                      <input
                        id="email"
                        type="email"
                        required
                        value={email}
                        onChange={e => setEmail(e.target.value)}
                        onFocus={() => setFocusedField('email')}
                        onBlur={() => setFocusedField(null)}
                        className="du-input du-input-bordered w-full h-14 pl-12 pr-4 bg-[#151b28]"
                        placeholder="you@werco.com"
                        autoComplete="email"
                      />
                    </div>
                  </div>

                  {/* Password field */}
                  <div className="space-y-2">
                    <label htmlFor="password" className="block text-sm font-medium text-slate-300">
                      Password
                    </label>
                    <div className="relative">
                      <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                        <LockClosedIcon
                          className={`h-5 w-5 transition-colors duration-200 ${
                            focusedField === 'password' ? 'text-werco-navy-600' : 'text-slate-400'
                          }`}
                        />
                      </div>
                      <input
                        id="password"
                        type={showPassword ? 'text' : 'password'}
                        required
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        onFocus={() => setFocusedField('password')}
                        onBlur={() => setFocusedField(null)}
                        className="du-input du-input-bordered w-full h-14 pl-12 pr-12 bg-[#151b28]"
                        placeholder="Enter your password"
                        autoComplete="current-password"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="absolute inset-y-0 right-0 pr-4 flex items-center text-slate-400 hover:text-slate-400 transition-colors"
                      >
                        {showPassword ? <EyeSlashIcon className="h-5 w-5" /> : <EyeIcon className="h-5 w-5" />}
                      </button>
                    </div>
                  </div>
                </>
              )}

              {loginMode === 'email' && (
                <div className="flex justify-end">
                  <button type="button" className="du-link du-link-primary text-sm">
                    Forgot password?
                  </button>
                </div>
              )}

              {/* Submit button */}
              <button
                type="submit"
                disabled={loading}
                className="du-btn du-btn-primary du-btn-block h-14 normal-case text-base font-semibold"
              >
                {loading ? (
                  <span className="flex items-center justify-center gap-3">
                    <span className="du-loading du-loading-spinner du-loading-sm" aria-hidden="true" />
                    Signing in...
                  </span>
                ) : (
                  <span className="flex items-center justify-center gap-2">
                    Sign In
                    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
                    </svg>
                  </span>
                )}
              </button>
            </form>

            {/* Create account link */}
            <div className="mt-6 text-center">
              <p className="text-sm text-slate-500">
                Don't have an account?{' '}
                <Link to="/register" className="du-link du-link-primary font-semibold">Create one</Link>
              </p>
            </div>

            {/* Security badge */}
            <div className="mt-6 pt-6 border-t border-slate-700/30">
              <div className="flex items-center justify-center gap-2 text-slate-400">
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                  <path
                    fillRule="evenodd"
                    d="M2.166 4.999A11.954 11.954 0 0010 1.944 11.954 11.954 0 0017.834 5c.11.65.166 1.32.166 2.001 0 5.225-3.34 9.67-8 11.317C5.34 16.67 2 12.225 2 7c0-.682.057-1.35.166-2.001zm11.541 3.708a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
                    clipRule="evenodd"
                  />
                </svg>
                <span className="text-xs">Secured with 256-bit encryption</span>
              </div>
            </div>
          </div>

          {/* Mobile compliance badges */}
          <div className="lg:hidden flex flex-wrap items-center justify-center gap-3 mt-8">
            <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-[#151b28]/80 border border-slate-700 text-slate-400">
              <ShieldCheckIcon className="h-4 w-4 text-werco-navy-600" />
              <span className="text-xs font-medium">AS9100D</span>
            </div>
            <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-[#151b28]/80 border border-slate-700 text-slate-400">
              <ShieldCheckIcon className="h-4 w-4 text-werco-navy-600" />
              <span className="text-xs font-medium">ISO 9001</span>
            </div>
            <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-[#151b28]/80 border border-slate-700 text-slate-400">
              <ShieldCheckIcon className="h-4 w-4 text-werco-navy-600" />
              <span className="text-xs font-medium">ITAR</span>
            </div>
          </div>

          {/* Mobile footer */}
          <p className="lg:hidden text-center text-slate-400 text-xs mt-6">
            &copy; 2026 Werco Manufacturing. All rights reserved.
          </p>
        </div>
      </div>
    </div>
  );
}
