import React, { useState, useEffect, useMemo } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ShieldCheckIcon, LockClosedIcon, EnvelopeIcon, EyeIcon, EyeSlashIcon } from '@heroicons/react/24/outline';
import { isKioskMode } from '../utils/kiosk';

// Animated particle component for background
const AnimatedParticles = () => {
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none">
      {/* Floating particles */}
      {[...Array(20)].map((_, i) => (
        <div
          key={i}
          className="absolute rounded-full bg-cyan-400/20"
          style={{
            width: Math.random() * 4 + 2 + 'px',
            height: Math.random() * 4 + 2 + 'px',
            left: Math.random() * 100 + '%',
            top: Math.random() * 100 + '%',
            animation: `float ${Math.random() * 10 + 15}s linear infinite`,
            animationDelay: `-${Math.random() * 10}s`,
          }}
        />
      ))}
      
      {/* Energy pulses */}
      <div className="absolute top-1/4 left-1/4 w-64 h-64 bg-cyan-500/10 rounded-full blur-3xl animate-pulse" />
      <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-blue-500/10 rounded-full blur-3xl animate-pulse" style={{ animationDelay: '1s' }} />
      <div className="absolute top-1/2 left-1/2 w-72 h-72 bg-amber-500/5 rounded-full blur-3xl animate-pulse" style={{ animationDelay: '2s' }} />
    </div>
  );
};

// Circuit pattern SVG background
const CircuitPattern = () => (
  <svg className="absolute inset-0 w-full h-full opacity-[0.03]" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <pattern id="circuit" x="0" y="0" width="100" height="100" patternUnits="userSpaceOnUse">
        <path d="M10 10h80M10 10v30M90 10v30M10 40h30M60 40h30M40 40v30M60 40v30M10 70h80M10 70v20M90 70v20" 
              stroke="currentColor" strokeWidth="1" fill="none" className="text-cyan-400"/>
        <circle cx="10" cy="10" r="3" className="fill-cyan-400"/>
        <circle cx="90" cy="10" r="3" className="fill-cyan-400"/>
        <circle cx="10" cy="40" r="2" className="fill-cyan-400"/>
        <circle cx="40" cy="40" r="2" className="fill-cyan-400"/>
        <circle cx="60" cy="40" r="2" className="fill-cyan-400"/>
        <circle cx="90" cy="40" r="2" className="fill-cyan-400"/>
        <circle cx="40" cy="70" r="2" className="fill-cyan-400"/>
        <circle cx="60" cy="70" r="2" className="fill-cyan-400"/>
        <circle cx="10" cy="70" r="3" className="fill-cyan-400"/>
        <circle cx="90" cy="70" r="3" className="fill-cyan-400"/>
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#circuit)" />
  </svg>
);

// Hexagon grid pattern
const HexagonGrid = () => (
  <svg className="absolute inset-0 w-full h-full opacity-[0.04]" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <pattern id="hexagons" width="56" height="100" patternUnits="userSpaceOnUse" patternTransform="scale(2)">
        <path d="M28 66L0 50L0 16L28 0L56 16L56 50L28 66L28 100" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-cyan-300"/>
        <path d="M28 0L28 34L0 50L0 84L28 100L56 84L56 50L28 34" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-cyan-300"/>
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#hexagons)" />
  </svg>
);

// Power flow lines animation
const PowerFlowLines = () => (
  <div className="absolute inset-0 overflow-hidden pointer-events-none">
    {[...Array(5)].map((_, i) => (
      <div
        key={i}
        className="absolute h-px bg-gradient-to-r from-transparent via-cyan-400/40 to-transparent"
        style={{
          width: '200%',
          top: `${20 + i * 15}%`,
          left: '-50%',
          animation: `flowLine ${8 + i * 2}s linear infinite`,
          animationDelay: `${i * 1.5}s`,
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
  const kioskMode = useMemo(() => isKioskMode(location.search), [location.search]);

  useEffect(() => {
    if (kioskMode) {
      setLoginMode('employee');
    }
  }, [kioskMode]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (loginMode === 'employee') {
        await loginWithEmployeeId(employeeId);
      } else {
        await login(email, password);
      }
      navigate('/');
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
        @keyframes float {
          0%, 100% { transform: translateY(0) translateX(0); opacity: 0; }
          10% { opacity: 1; }
          90% { opacity: 1; }
          100% { transform: translateY(-100vh) translateX(20px); opacity: 0; }
        }
        @keyframes flowLine {
          0% { transform: translateX(-50%); }
          100% { transform: translateX(50%); }
        }
        @keyframes gridPulse {
          0%, 100% { opacity: 0.03; }
          50% { opacity: 0.06; }
        }
        @keyframes glowPulse {
          0%, 100% { box-shadow: 0 0 20px rgba(6, 182, 212, 0.3), 0 0 40px rgba(6, 182, 212, 0.1); }
          50% { box-shadow: 0 0 30px rgba(6, 182, 212, 0.5), 0 0 60px rgba(6, 182, 212, 0.2); }
        }
        .input-glow:focus {
          box-shadow: 0 0 0 3px rgba(6, 182, 212, 0.2), 0 0 20px rgba(6, 182, 212, 0.1);
        }
        .btn-glow {
          background: linear-gradient(135deg, #0891b2 0%, #0e7490 50%, #0d9488 100%);
          box-shadow: 0 4px 15px rgba(6, 182, 212, 0.4);
          transition: all 0.3s ease;
        }
        .btn-glow:hover {
          box-shadow: 0 6px 25px rgba(6, 182, 212, 0.6);
          transform: translateY(-1px);
        }
        .btn-glow:active {
          transform: translateY(0);
        }
        .glass-card {
          background: rgba(255, 255, 255, 0.95);
          backdrop-filter: blur(20px);
          border: 1px solid rgba(255, 255, 255, 0.2);
        }
      `}</style>

      {/* Left side - Branding with animated background */}
      <div className="hidden lg:flex lg:w-1/2 relative overflow-hidden">
        {/* Deep navy gradient background */}
        <div className="absolute inset-0 bg-gradient-to-br from-slate-900 via-blue-950 to-slate-900" />
        
        {/* Animated elements */}
        <HexagonGrid />
        <CircuitPattern />
        <PowerFlowLines />
        <AnimatedParticles />
        
        {/* Radial gradient overlay */}
        <div className="absolute inset-0 bg-gradient-to-t from-slate-900/80 via-transparent to-slate-900/40" />
        
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
            {/* Main heading with gradient text */}
            <div>
              <h1 className="text-5xl font-bold leading-tight">
                <span className="text-white">Manufacturing</span>
                <br />
                <span className="bg-gradient-to-r from-cyan-400 via-cyan-500 to-blue-400 bg-clip-text text-transparent">
                  Execution System
                </span>
              </h1>
            </div>
            
            {/* Tagline */}
            <p className="text-lg text-slate-300 max-w-md leading-relaxed">
              Precision fabrication management for{' '}
              <span className="text-cyan-400 font-medium">power generation</span>,{' '}
              <span className="text-cyan-400 font-medium">data center</span>, and{' '}
              <span className="text-cyan-400 font-medium">defense</span> sectors.
            </p>
            
            {/* Certification badges */}
            <div className="flex flex-wrap items-center gap-4 pt-4">
              <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 border border-white/10 backdrop-blur-sm">
                <ShieldCheckIcon className="h-5 w-5 text-cyan-400" />
                <span className="text-sm font-medium text-white">AS9100D Compliant</span>
              </div>
              <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 border border-white/10 backdrop-blur-sm">
                <ShieldCheckIcon className="h-5 w-5 text-red-400" />
                <span className="text-sm font-medium text-white">ISO 9001 Certified</span>
              </div>
            </div>
            
            {/* Decorative stats/metrics */}
            <div className="grid grid-cols-3 gap-6 pt-8 border-t border-white/10">
              <div>
                <div className="text-3xl font-bold text-cyan-400">99.9%</div>
                <div className="text-sm text-slate-400 mt-1">System Uptime</div>
              </div>
              <div>
                <div className="text-3xl font-bold text-red-400">24/7</div>
                <div className="text-sm text-slate-400 mt-1">Operations</div>
              </div>
              <div>
                <div className="text-3xl font-bold text-blue-400">100%</div>
                <div className="text-sm text-slate-400 mt-1">Traceability</div>
              </div>
            </div>
          </div>
          
          <p className="text-slate-500 text-sm">
            &copy; 2026 Werco Manufacturing. All rights reserved.
          </p>
        </div>
      </div>

      {/* Right side - Login form */}
      <div className="flex-1 flex items-center justify-center bg-gradient-to-br from-slate-50 via-slate-100 to-blue-50 p-8 relative">
        {/* Subtle background pattern for right side */}
        <div className="absolute inset-0 opacity-[0.02]">
          <svg className="w-full h-full" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <pattern id="dots" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse">
                <circle cx="2" cy="2" r="1" className="fill-slate-900"/>
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#dots)" />
          </svg>
        </div>
        
        <div className="w-full max-w-md relative z-10">
          {/* Mobile logo */}
          <div className="lg:hidden text-center mb-8">
            <img 
              src="/Werco_Logo-PNG.png" 
              alt="Werco Manufacturing" 
              className="h-14 mx-auto mb-2" 
            />
            <p className="text-slate-500 text-sm">Manufacturing Execution System</p>
          </div>

          {/* Login card with glassmorphism */}
          <div className="glass-card rounded-3xl shadow-2xl p-10 relative overflow-hidden">
            {/* Subtle gradient accent at top */}
            <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-cyan-500 via-cyan-600 to-blue-500" />
            
            <div className="text-center mb-8">
              {/* Animated lock icon container */}
              <div className="relative w-16 h-16 mx-auto mb-5">
                <div className="absolute inset-0 bg-gradient-to-br from-cyan-500 to-cyan-600 rounded-2xl rotate-3 opacity-20" />
                <div className="absolute inset-0 bg-gradient-to-br from-cyan-500 to-cyan-600 rounded-2xl -rotate-3 opacity-20" />
                <div className="relative w-full h-full bg-gradient-to-br from-slate-800 to-slate-900 rounded-2xl flex items-center justify-center shadow-lg">
                  <LockClosedIcon className="h-8 w-8 text-cyan-400" />
                </div>
              </div>
              <h2 className="text-2xl font-bold text-slate-800">Welcome back</h2>
              <p className="text-slate-500 mt-2">Sign in to access your dashboard</p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-6">
              <div className={`flex items-center justify-center gap-2 bg-slate-50 border border-slate-200 rounded-xl p-1 ${kioskMode ? 'opacity-70 pointer-events-none' : ''}`}>
                <button
                  type="button"
                  onClick={() => setLoginMode('employee')}
                  className={`flex-1 px-3 py-2 text-sm font-semibold rounded-lg transition-colors ${
                    loginMode === 'employee' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  Employee ID
                </button>
                <button
                  type="button"
                  onClick={() => setLoginMode('email')}
                  className={`flex-1 px-3 py-2 text-sm font-semibold rounded-lg transition-colors ${
                    loginMode === 'email' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  Email Login
                </button>
              </div>

              {error && (
                <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-xl flex items-center gap-2 animate-fade-in">
                  <svg className="w-5 h-5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                  </svg>
                  <span className="text-sm">{error}</span>
                </div>
              )}

              {loginMode === 'employee' ? (
                <div className="space-y-2">
                  <label htmlFor="employeeId" className="block text-sm font-medium text-slate-700">
                    Employee ID (4 digits)
                  </label>
                  <div className="relative">
                    <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                      <LockClosedIcon className={`h-5 w-5 transition-colors duration-200 ${
                        focusedField === 'employeeId' ? 'text-cyan-500' : 'text-slate-400'
                      }`} />
                    </div>
                    <input
                      id="employeeId"
                      type="text"
                      inputMode="numeric"
                      pattern="\\d{4}"
                      maxLength={4}
                      required
                      value={employeeId}
                      onChange={(e) => setEmployeeId(e.target.value.replace(/\\D/g, '').slice(0, 4))}
                      onFocus={() => setFocusedField('employeeId')}
                      onBlur={() => setFocusedField(null)}
                      className="input-glow w-full pl-12 pr-4 py-3.5 bg-white border-2 border-slate-200 rounded-xl text-slate-800 placeholder-slate-400 focus:border-cyan-500 focus:outline-none transition-all duration-200 tracking-widest text-center text-lg"
                      placeholder="0000"
                      autoComplete="off"
                    />
                  </div>
                  <p className="text-xs text-slate-500">Use your 4-digit badge ID.</p>
                </div>
              ) : (
                <>
                  {/* Email field */}
                  <div className="space-y-2">
                    <label htmlFor="email" className="block text-sm font-medium text-slate-700">
                      Email Address
                    </label>
                    <div className="relative">
                      <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                        <EnvelopeIcon className={`h-5 w-5 transition-colors duration-200 ${
                          focusedField === 'email' ? 'text-cyan-500' : 'text-slate-400'
                        }`} />
                      </div>
                      <input
                        id="email"
                        type="email"
                        required
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        onFocus={() => setFocusedField('email')}
                        onBlur={() => setFocusedField(null)}
                        className="input-glow w-full pl-12 pr-4 py-3.5 bg-white border-2 border-slate-200 rounded-xl text-slate-800 placeholder-slate-400 focus:border-cyan-500 focus:outline-none transition-all duration-200"
                        placeholder="you@werco.com"
                        autoComplete="email"
                      />
                    </div>
                  </div>

                  {/* Password field */}
                  <div className="space-y-2">
                    <label htmlFor="password" className="block text-sm font-medium text-slate-700">
                      Password
                    </label>
                    <div className="relative">
                      <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                        <LockClosedIcon className={`h-5 w-5 transition-colors duration-200 ${
                          focusedField === 'password' ? 'text-cyan-500' : 'text-slate-400'
                        }`} />
                      </div>
                      <input
                        id="password"
                        type={showPassword ? 'text' : 'password'}
                        required
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        onFocus={() => setFocusedField('password')}
                        onBlur={() => setFocusedField(null)}
                        className="input-glow w-full pl-12 pr-12 py-3.5 bg-white border-2 border-slate-200 rounded-xl text-slate-800 placeholder-slate-400 focus:border-cyan-500 focus:outline-none transition-all duration-200"
                        placeholder="Enter your password"
                        autoComplete="current-password"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="absolute inset-y-0 right-0 pr-4 flex items-center text-slate-400 hover:text-slate-600 transition-colors"
                      >
                        {showPassword ? (
                          <EyeSlashIcon className="h-5 w-5" />
                        ) : (
                          <EyeIcon className="h-5 w-5" />
                        )}
                      </button>
                    </div>
                  </div>
                </>
              )}

              {loginMode === 'email' && (
                <div className="flex justify-end">
                  <button
                    type="button"
                    className="text-sm text-cyan-600 hover:text-cyan-700 font-medium transition-colors"
                  >
                    Forgot password?
                  </button>
                </div>
              )}

              {/* Submit button */}
              <button
                type="submit"
                disabled={loading}
                className="btn-glow w-full py-4 text-base font-semibold text-white rounded-xl disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {loading ? (
                  <span className="flex items-center justify-center gap-3">
                    <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                    </svg>
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
            
            {/* Security badge */}
            <div className="mt-8 pt-6 border-t border-slate-100">
              <div className="flex items-center justify-center gap-2 text-slate-400">
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M2.166 4.999A11.954 11.954 0 0010 1.944 11.954 11.954 0 0017.834 5c.11.65.166 1.32.166 2.001 0 5.225-3.34 9.67-8 11.317C5.34 16.67 2 12.225 2 7c0-.682.057-1.35.166-2.001zm11.541 3.708a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                </svg>
                <span className="text-xs">Secured with 256-bit encryption</span>
              </div>
            </div>
          </div>

          {/* Mobile compliance badges */}
          <div className="lg:hidden flex flex-wrap items-center justify-center gap-3 mt-8">
            <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/80 border border-slate-200 text-slate-600">
              <ShieldCheckIcon className="h-4 w-4 text-cyan-500" />
              <span className="text-xs font-medium">AS9100D</span>
            </div>
            <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/80 border border-slate-200 text-slate-600">
              <ShieldCheckIcon className="h-4 w-4 text-cyan-500" />
              <span className="text-xs font-medium">ISO 9001</span>
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
