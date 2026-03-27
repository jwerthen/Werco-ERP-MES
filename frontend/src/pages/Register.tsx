import React, { useState, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import api from '../services/api';
import {
  ShieldCheckIcon,
  LockClosedIcon,
  EnvelopeIcon,
  EyeIcon,
  EyeSlashIcon,
  UserIcon,
  IdentificationIcon,
} from '@heroicons/react/24/outline';

// Blueprint grid pattern - matches Login page
const BlueprintGrid = () => (
  <svg className="absolute inset-0 w-full h-full opacity-[0.04]" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <pattern id="blueprint-grid" width="40" height="40" patternUnits="userSpaceOnUse">
        <path d="M0 0h40v40H0z" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-blue-300" />
        <path d="M20 0v40M0 20h40" fill="none" stroke="currentColor" strokeWidth="0.25" className="text-blue-300" />
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#blueprint-grid)" />
  </svg>
);

const AnimatedBackground = () => (
  <div className="absolute inset-0 overflow-hidden pointer-events-none">
    <div className="absolute top-1/4 left-1/4 w-64 h-64 bg-blue-500/8 rounded-full blur-3xl animate-pulse" />
    <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-blue-600/6 rounded-full blur-3xl animate-pulse" style={{ animationDelay: '1s' }} />
    <div className="absolute top-1/2 left-1/2 w-72 h-72 bg-blue-400/4 rounded-full blur-3xl animate-pulse" style={{ animationDelay: '2s' }} />
    {[...Array(3)].map((_, i) => (
      <div
        key={i}
        className="absolute h-px bg-gradient-to-r from-transparent via-blue-400/20 to-transparent"
        style={{ width: '200%', top: `${15 + i * 12}%`, left: '-50%', animation: `flowLine ${10 + i * 3}s linear infinite`, animationDelay: `${i * 2}s` }}
      />
    ))}
  </div>
);

export default function Register() {
  const [firstName, setFirstName] = useState('');
  const [lastName, setLastName] = useState('');
  const [email, setEmail] = useState('');
  const [employeeId, setEmployeeId] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState<{ message: string; isFirstUser: boolean } | null>(null);
  const [isSetupMode, setIsSetupMode] = useState(false);
  const [focusedField, setFocusedField] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    api.getSetupStatus().then((res) => {
      setIsSetupMode(res.is_setup_required);
    }).catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    setLoading(true);
    try {
      const result = await api.registerPublic({
        first_name: firstName,
        last_name: lastName,
        email,
        employee_id: employeeId,
        password,
      });
      setSuccess({ message: result.message, isFirstUser: result.is_first_user });
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

  const inputClass = (field: string) =>
    `du-input du-input-bordered w-full h-12 pl-12 pr-4 bg-white ${focusedField === field ? 'du-input-primary' : ''}`;

  const iconClass = (field: string) =>
    `h-5 w-5 transition-colors duration-200 ${focusedField === field ? 'text-werco-navy-600' : 'text-slate-400'}`;

  if (success) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-50 via-white to-blue-50/40 p-8">
        <div className="glass-card rounded-3xl shadow-xl p-10 max-w-md w-full text-center relative overflow-hidden">
          <style>{`.glass-card { background: rgba(255, 255, 255, 0.97); backdrop-filter: blur(20px); border: 1px solid rgba(226, 232, 240, 0.8); }`}</style>
          <div className="absolute top-0 left-0 right-0 h-1" style={{ background: 'linear-gradient(90deg, #1B4D9C 0%, #3366FF 50%, #1B4D9C 100%)' }} />
          <div className="w-16 h-16 mx-auto mb-6 rounded-2xl flex items-center justify-center" style={{ background: 'linear-gradient(135deg, #0a1628 0%, #0f2952 100%)' }}>
            <ShieldCheckIcon className="h-8 w-8 text-green-400" />
          </div>
          <h2 className="text-2xl font-bold text-slate-800 mb-3">
            {success.isFirstUser ? 'System Ready' : 'Request Submitted'}
          </h2>
          <p className="text-slate-600 mb-8">{success.message}</p>
          <button onClick={() => navigate('/login')} className="du-btn du-btn-primary du-btn-block h-12 normal-case text-base font-semibold">
            Go to Sign In
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex">
      <style>{`
        @keyframes flowLine { 0% { transform: translateX(-50%); } 100% { transform: translateX(50%); } }
        .glass-card { background: rgba(255, 255, 255, 0.97); backdrop-filter: blur(20px); border: 1px solid rgba(226, 232, 240, 0.8); }
      `}</style>

      {/* Left side - Werco Branding */}
      <div className="hidden lg:flex lg:w-1/2 relative overflow-hidden">
        <div className="absolute inset-0" style={{ background: 'linear-gradient(135deg, #0a1628 0%, #0f2952 40%, #123266 60%, #0a1628 100%)' }} />
        <BlueprintGrid />
        <AnimatedBackground />
        <div className="absolute inset-0 bg-gradient-to-t from-[#0a1628]/80 via-transparent to-[#0a1628]/40" />
        <div className="relative z-10 p-12 flex flex-col justify-between w-full">
          <div>
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-14 brightness-0 invert drop-shadow-lg" />
          </div>
          <div className="space-y-8">
            <div>
              <h1 className="text-5xl font-bold leading-tight">
                <span className="text-white">Manufacturing</span><br />
                <span className="bg-gradient-to-r from-blue-300 via-blue-400 to-white bg-clip-text text-transparent">Execution System</span>
              </h1>
            </div>
            <p className="text-lg text-slate-300 max-w-md leading-relaxed">
              Built for What Flies, Fights, and Powers the Future.
            </p>
            <div className="flex flex-wrap items-center gap-3 pt-4">
              {['AS9100D', 'ISO 9001', 'ITAR'].map((cert) => (
                <div key={cert} className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/[0.04] border border-white/[0.08]">
                  <ShieldCheckIcon className="h-4 w-4 text-blue-400" />
                  <span className="text-sm font-medium text-white/90">{cert}</span>
                </div>
              ))}
            </div>
          </div>
          <p className="text-slate-500 text-sm">&copy; 2026 Werco Manufacturing. All rights reserved.</p>
        </div>
      </div>

      {/* Right side - Registration form */}
      <div className="flex-1 flex items-center justify-center bg-gradient-to-br from-slate-50 via-white to-blue-50/40 p-8 relative overflow-y-auto">
        <div className="absolute inset-0 opacity-[0.02]">
          <svg className="w-full h-full" xmlns="http://www.w3.org/2000/svg">
            <defs><pattern id="dots" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1" className="fill-slate-900" /></pattern></defs>
            <rect width="100%" height="100%" fill="url(#dots)" />
          </svg>
        </div>

        <div className="w-full max-w-md relative z-10">
          <div className="lg:hidden text-center mb-6">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-14 mx-auto mb-2" />
            <p className="text-slate-500 text-sm">Manufacturing Execution System</p>
          </div>

          <div className="glass-card rounded-3xl shadow-xl p-10 relative overflow-hidden">
            <div className="absolute top-0 left-0 right-0 h-1" style={{ background: 'linear-gradient(90deg, #1B4D9C 0%, #3366FF 50%, #1B4D9C 100%)' }} />

            <div className="text-center mb-6">
              <div className="relative w-16 h-16 mx-auto mb-5">
                <div className="absolute inset-0 bg-gradient-to-br from-werco-navy-600 to-blue-700 rounded-2xl rotate-3 opacity-20" />
                <div className="absolute inset-0 bg-gradient-to-br from-werco-navy-600 to-blue-700 rounded-2xl -rotate-3 opacity-20" />
                <div className="relative w-full h-full rounded-2xl flex items-center justify-center shadow-lg" style={{ background: 'linear-gradient(135deg, #0a1628 0%, #0f2952 100%)' }}>
                  <UserIcon className="h-8 w-8 text-blue-300" />
                </div>
              </div>
              <h2 className="text-2xl font-bold text-slate-800">
                {isSetupMode ? 'System Setup' : 'Create Account'}
              </h2>
              <p className="text-slate-500 mt-2">
                {isSetupMode ? 'Create the administrator account' : 'Set up your Werco ERP account'}
              </p>
              {!isSetupMode && (
                <p className="text-xs text-amber-600 mt-2">Your account will require admin approval before you can sign in.</p>
              )}
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">
              {error && (
                <div className="du-alert du-alert-error animate-fade-in text-sm">
                  <svg className="w-5 h-5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                  </svg>
                  <span>{error}</span>
                </div>
              )}

              {/* Name row */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="firstName" className="block text-sm font-medium text-slate-700 mb-1">First Name</label>
                  <div className="relative">
                    <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                      <UserIcon className={iconClass('firstName')} />
                    </div>
                    <input id="firstName" type="text" required value={firstName} onChange={(e) => setFirstName(e.target.value)}
                      onFocus={() => setFocusedField('firstName')} onBlur={() => setFocusedField(null)}
                      className={inputClass('firstName')} placeholder="John" autoComplete="given-name" />
                  </div>
                </div>
                <div>
                  <label htmlFor="lastName" className="block text-sm font-medium text-slate-700 mb-1">Last Name</label>
                  <div className="relative">
                    <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                      <UserIcon className={iconClass('lastName')} />
                    </div>
                    <input id="lastName" type="text" required value={lastName} onChange={(e) => setLastName(e.target.value)}
                      onFocus={() => setFocusedField('lastName')} onBlur={() => setFocusedField(null)}
                      className={inputClass('lastName')} placeholder="Doe" autoComplete="family-name" />
                  </div>
                </div>
              </div>

              {/* Email */}
              <div>
                <label htmlFor="email" className="block text-sm font-medium text-slate-700 mb-1">Email Address</label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <EnvelopeIcon className={iconClass('email')} />
                  </div>
                  <input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
                    onFocus={() => setFocusedField('email')} onBlur={() => setFocusedField(null)}
                    className={inputClass('email')} placeholder="you@wercomfg.com" autoComplete="email" />
                </div>
              </div>

              {/* Employee ID */}
              <div>
                <label htmlFor="employeeId" className="block text-sm font-medium text-slate-700 mb-1">Employee ID</label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <IdentificationIcon className={iconClass('employeeId')} />
                  </div>
                  <input id="employeeId" type="text" required value={employeeId}
                    onChange={(e) => setEmployeeId(e.target.value.replace(/[^A-Za-z0-9\-_]/g, '').slice(0, 50))}
                    onFocus={() => setFocusedField('employeeId')} onBlur={() => setFocusedField(null)}
                    className={inputClass('employeeId')} placeholder="EMP-001" autoComplete="off" />
                </div>
              </div>

              {/* Password */}
              <div>
                <label htmlFor="password" className="block text-sm font-medium text-slate-700 mb-1">Password</label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <LockClosedIcon className={iconClass('password')} />
                  </div>
                  <input id="password" type={showPassword ? 'text' : 'password'} required value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    onFocus={() => setFocusedField('password')} onBlur={() => setFocusedField(null)}
                    className={`${inputClass('password')} pr-12`} placeholder="Min 12 characters" autoComplete="new-password" />
                  <button type="button" onClick={() => setShowPassword(!showPassword)}
                    className="absolute inset-y-0 right-0 pr-4 flex items-center text-slate-400 hover:text-slate-600 transition-colors">
                    {showPassword ? <EyeSlashIcon className="h-5 w-5" /> : <EyeIcon className="h-5 w-5" />}
                  </button>
                </div>
                <p className="text-xs text-slate-400 mt-1">Uppercase, lowercase, number, and special character required.</p>
              </div>

              {/* Confirm Password */}
              <div>
                <label htmlFor="confirmPassword" className="block text-sm font-medium text-slate-700 mb-1">Confirm Password</label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <LockClosedIcon className={iconClass('confirmPassword')} />
                  </div>
                  <input id="confirmPassword" type={showPassword ? 'text' : 'password'} required value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    onFocus={() => setFocusedField('confirmPassword')} onBlur={() => setFocusedField(null)}
                    className={inputClass('confirmPassword')} placeholder="Re-enter password" autoComplete="new-password" />
                </div>
              </div>

              {/* Submit */}
              <button type="submit" disabled={loading} className="du-btn du-btn-primary du-btn-block h-12 normal-case text-base font-semibold mt-2">
                {loading ? (
                  <span className="flex items-center justify-center gap-3">
                    <span className="du-loading du-loading-spinner du-loading-sm" aria-hidden="true" />
                    Creating account...
                  </span>
                ) : (
                  isSetupMode ? 'Create Admin Account' : 'Request Account'
                )}
              </button>
            </form>

            <div className="mt-6 pt-4 border-t border-slate-100 text-center">
              <p className="text-sm text-slate-500">
                Already have an account?{' '}
                <Link to="/login" className="du-link du-link-primary font-semibold">Sign in</Link>
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
