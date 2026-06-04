import React, { useState, useEffect, useMemo } from 'react';
import { useNavigate, useLocation, Link } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import {
  ShieldCheckIcon,
  LockClosedIcon,
  EnvelopeIcon,
  EyeIcon,
  EyeSlashIcon,
  ArrowRightIcon,
} from '@heroicons/react/24/outline';

// Foundry blueprint grid texture
const gridTex: React.CSSProperties = {
  backgroundImage:
    'linear-gradient(rgba(36,48,68,.25) 1px,transparent 1px),linear-gradient(90deg,rgba(36,48,68,.25) 1px,transparent 1px)',
  backgroundSize: '28px 28px',
};

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [employeeId, setEmployeeId] = useState('');
  const [loginMode, setLoginMode] = useState<'employee' | 'email'>('email');
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

  const fieldFocusRing = (field: string): React.CSSProperties =>
    focusedField === field
      ? { borderColor: 'var(--fd-blue)', boxShadow: '0 0 0 3px rgba(47,129,247,0.12)' }
      : { borderColor: 'var(--fd-line)' };

  return (
    <div className="min-h-screen flex font-sans" style={{ background: 'var(--fd-canvas)' }}>
      {/* Left — Brand instrument panel */}
      <div
        className="hidden lg:block lg:w-[52%] relative overflow-hidden"
        style={{ background: 'var(--fd-panel)', borderRight: '1px solid var(--fd-line)' }}
      >
        <div className="absolute inset-0 opacity-50" style={gridTex} />
        <div
          className="absolute"
          style={{
            top: '15%',
            left: '12%',
            width: 320,
            height: 320,
            borderRadius: '50%',
            background: 'rgba(47,129,247,.1)',
            filter: 'blur(90px)',
          }}
        />

        <div className="relative z-10 h-full p-12 flex flex-col justify-between box-border">
          {/* Logo + live chip */}
          <div className="flex items-center gap-3">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-[30px] brightness-0 invert" />
            <span
              className="font-mono text-[10px] tracking-[0.1em] text-fd-green px-1.5 py-1 rounded-[3px]"
              style={{ border: '1px solid var(--fd-line)' }}
            >
              ● SYSTEM LIVE
            </span>
          </div>

          {/* Headline + metrics */}
          <div>
            <div className="font-mono text-xs text-fd-blue tracking-[0.2em] mb-[18px]">ERP // MES</div>
            <h1 className="text-[44px] font-extrabold leading-[1.05] tracking-[-0.02em] m-0 text-fd-ink">
              Built for What Flies,
              <br />
              Fights &amp; Powers
              <br />
              <span className="text-fd-blue">the Future.</span>
            </h1>

            <div className="grid grid-cols-3 gap-2.5 mt-8 max-w-[440px]">
              {[
                ['24H', 'RFQ RESPONSE'],
                ['95%+', 'ON-TIME'],
                ['99%+', 'FIRST-PASS'],
              ].map(([v, l]) => (
                <div
                  key={l}
                  className="p-3.5 rounded-[3px]"
                  style={{ border: '1px solid var(--fd-line)', background: 'var(--fd-raised)' }}
                >
                  <div className="font-mono text-2xl font-bold text-fd-ink tabular-nums">{v}</div>
                  <div className="font-mono text-[9.5px] text-fd-mute tracking-[0.1em] mt-1.5">{l}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="font-mono text-[11px] text-fd-faint tracking-[0.12em]">
            AS9100D · ISO 9001 · CMMC L2 · ITAR
          </div>
        </div>
      </div>

      {/* Right — Form */}
      <div className="flex-1 flex items-center justify-center p-8 relative">
        <div className="absolute inset-0 opacity-[0.25]" style={gridTex} aria-hidden="true" />
        <div className="w-full max-w-[380px] relative z-10">
          {/* Mobile logo */}
          <div className="lg:hidden text-center mb-8">
            <img src="/Werco_Logo-PNG.png" alt="Werco Manufacturing" className="h-12 mx-auto mb-2 brightness-0 invert" />
            <p className="font-mono text-[11px] tracking-[0.18em] text-fd-mute uppercase">ERP // MES</p>
          </div>

          {/* Auth card */}
          <div
            className="relative p-9"
            style={{ background: 'var(--fd-panel)', border: '1px solid var(--fd-line)', borderRadius: 'var(--fd-radius-lg)' }}
          >
            {/* Top accent bar */}
            <div className="absolute top-[-1px] left-[-1px] right-[-1px] h-0.5" style={{ background: 'var(--fd-blue)' }} />

            <div className="font-mono text-[11px] text-fd-mute tracking-[0.18em]">AUTHENTICATE</div>
            <h2 className="text-2xl font-bold text-fd-ink mt-2.5 mb-1">Sign in</h2>
            <p className="text-[13.5px] text-fd-mute mb-6">Access your production dashboard.</p>

            <form onSubmit={handleSubmit} className="flex flex-col gap-[18px]">
              {/* Mode toggle */}
              <div
                className={`grid grid-cols-2 overflow-hidden rounded-[3px] ${forceEmployeeMode ? 'opacity-70 pointer-events-none' : ''}`}
                style={{ border: '1px solid var(--fd-line)' }}
              >
                {(['employee', 'email'] as const).map(m => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setLoginMode(m)}
                    className="py-2.5 px-2 font-mono text-[11px] font-semibold tracking-[0.08em] uppercase transition-colors"
                    style={
                      loginMode === m
                        ? { background: 'var(--fd-blue)', color: '#04101f' }
                        : { background: 'transparent', color: 'var(--fd-mute)' }
                    }
                  >
                    {m === 'employee' ? 'Badge ID' : 'Email'}
                  </button>
                ))}
              </div>

              {error && (
                <div className="alert alert-danger animate-fade-in text-sm">
                  <span>{error}</span>
                </div>
              )}

              {loginMode === 'employee' ? (
                <div>
                  <label
                    htmlFor="employeeId"
                    className="block font-mono text-[10px] uppercase tracking-[0.14em] text-fd-body mb-1.5"
                  >
                    Employee / Badge ID
                  </label>
                  <div className="relative">
                    <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                      <LockClosedIcon
                        className="h-[17px] w-[17px]"
                        style={{ color: focusedField === 'employeeId' ? 'var(--fd-blue)' : 'var(--fd-faint)' }}
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
                      className="w-full font-mono text-base text-fd-ink rounded-[3px] pl-10 pr-4 py-2.5 outline-none transition-all min-h-[44px]"
                      style={{ background: 'var(--fd-sunken)', border: '1px solid', ...fieldFocusRing('employeeId') }}
                      placeholder="0000 or EMP-1001"
                      autoComplete="off"
                    />
                  </div>
                  <p className="font-mono text-[9.5px] tracking-[0.1em] text-fd-mute mt-1.5">
                    Use your employee ID or 4-digit badge ID.
                  </p>
                </div>
              ) : (
                <>
                  <div>
                    <label
                      htmlFor="email"
                      className="block font-mono text-[10px] uppercase tracking-[0.14em] text-fd-body mb-1.5"
                    >
                      Email
                    </label>
                    <div className="relative">
                      <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                        <EnvelopeIcon
                          className="h-[17px] w-[17px]"
                          style={{ color: focusedField === 'email' ? 'var(--fd-blue)' : 'var(--fd-faint)' }}
                        />
                      </div>
                      <input
                        id="email"
                        name="email"
                        type="email"
                        required
                        value={email}
                        onChange={e => setEmail(e.target.value)}
                        onFocus={() => setFocusedField('email')}
                        onBlur={() => setFocusedField(null)}
                        className="w-full font-mono text-base text-fd-ink rounded-[3px] pl-10 pr-4 py-2.5 outline-none transition-all min-h-[44px]"
                        style={{ background: 'var(--fd-sunken)', border: '1px solid', ...fieldFocusRing('email') }}
                        placeholder="you@werco.com"
                        autoComplete="email"
                      />
                    </div>
                  </div>

                  <div>
                    <label
                      htmlFor="password"
                      className="block font-mono text-[10px] uppercase tracking-[0.14em] text-fd-body mb-1.5"
                    >
                      Password
                    </label>
                    <div className="relative">
                      <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                        <LockClosedIcon
                          className="h-[17px] w-[17px]"
                          style={{ color: focusedField === 'password' ? 'var(--fd-blue)' : 'var(--fd-faint)' }}
                        />
                      </div>
                      <input
                        id="password"
                        name="password"
                        type={showPassword ? 'text' : 'password'}
                        required
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        onFocus={() => setFocusedField('password')}
                        onBlur={() => setFocusedField(null)}
                        className="w-full font-mono text-base text-fd-ink rounded-[3px] pl-10 pr-10 py-2.5 outline-none transition-all min-h-[44px]"
                        style={{ background: 'var(--fd-sunken)', border: '1px solid', ...fieldFocusRing('password') }}
                        placeholder="Enter your password"
                        autoComplete="current-password"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="absolute inset-y-0 right-0 pr-3 flex items-center text-fd-faint hover:text-fd-body transition-colors"
                      >
                        {showPassword ? <EyeSlashIcon className="h-[18px] w-[18px]" /> : <EyeIcon className="h-[18px] w-[18px]" />}
                      </button>
                    </div>
                  </div>
                </>
              )}

              {loginMode === 'email' && (
                <div className="flex justify-end -mt-2">
                  <button type="button" className="font-mono text-[11px] text-fd-blue hover:text-blue-400 transition-colors">
                    Forgot password?
                  </button>
                </div>
              )}

              <button type="submit" disabled={loading} className="btn-primary btn-block w-full">
                {loading ? (
                  <span className="flex items-center justify-center gap-2">
                    <span className="spinner h-4 w-4" aria-hidden="true" />
                    Signing in…
                  </span>
                ) : (
                  <span className="flex items-center justify-center gap-2">
                    Sign In
                    <ArrowRightIcon className="w-4 h-4" />
                  </span>
                )}
              </button>
            </form>

            {/* Create account */}
            <div className="mt-5 text-center">
              <p className="text-sm text-fd-mute">
                Don&apos;t have an account?{' '}
                <Link to="/register" className="text-fd-blue font-semibold hover:text-blue-400 transition-colors">
                  Create one
                </Link>
              </p>
            </div>

            {/* Security strip */}
            <div
              className="mt-5 pt-4 flex items-center justify-center gap-2 text-fd-mute"
              style={{ borderTop: '1px solid var(--fd-line)' }}
            >
              <ShieldCheckIcon className="h-[15px] w-[15px]" />
              <span className="font-mono text-[10.5px] tracking-[0.08em]">256-BIT ENCRYPTED</span>
            </div>
          </div>

          {/* Mobile compliance */}
          <div className="lg:hidden mt-6 text-center font-mono text-[10px] tracking-[0.14em] text-fd-faint">
            AS9100D · ISO 9001 · CMMC L2 · ITAR
          </div>
        </div>
      </div>
    </div>
  );
}
