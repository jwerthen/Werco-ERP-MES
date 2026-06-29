import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';
import QRCode from 'qrcode';
import api from '../services/api';
import { UserRole } from '../types';
import { ErrorState } from '../components/ui';

/**
 * A0.4 badge print sheet — /print/badges?user_ids=1,2,3
 *
 * Prints CR80-sized (3.375in x 2.125in) employee badges, two per row, for the
 * users selected on the Users admin page. RBAC mirrors the Users page
 * (users:view route requirement in App.tsx).
 *
 * Symbology decision: the badge encodes `users.employee_id` as a QR code via
 * the qrcode dependency the traveler already uses — zero new dependencies.
 * Wedge scanners on the floor are 2D imagers that read QR and Code128 alike,
 * and both /auth/employee-login and /scanner/resolve-action take the decoded
 * text, so Code128 (which would add a jsbarcode dependency) buys nothing here.
 */

interface BadgeUser {
  id: number;
  employee_id: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  department?: string;
  is_active: boolean;
}

const roleLabels: Record<UserRole, string> = {
  platform_admin: 'Platform Admin',
  admin: 'Administrator',
  manager: 'Manager',
  supervisor: 'Supervisor',
  operator: 'Operator',
  quality: 'Quality',
  shipping: 'Shipping',
  viewer: 'View Only',
};

export default function PrintBadges() {
  const location = useLocation();
  const [users, setUsers] = useState<BadgeUser[]>([]);
  const [qrDataUrls, setQrDataUrls] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // True only for an actual fetch failure (retryable); validation messages
  // like "no users selected" are not retryable, so no Retry button there.
  const [loadFailed, setLoadFailed] = useState(false);

  const requestedIds = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return (params.get('user_ids') || '')
      .split(',')
      .map((value) => Number(value.trim()))
      .filter((value) => Number.isInteger(value) && value > 0);
  }, [location.search]);

  const loadUsers = useCallback(async () => {
    setLoadFailed(false);
    setError('');
    setLoading(true);
    if (requestedIds.length === 0) {
      setError('No users selected. Open this page from the Users screen via "Print badges".');
      setLoading(false);
      return;
    }
    try {
      const allUsers: BadgeUser[] = await api.getUsers(true);
      const wanted = new Set(requestedIds);
      const selected = allUsers.filter((u) => wanted.has(u.id));
      // Preserve the requested order.
      selected.sort((a, b) => requestedIds.indexOf(a.id) - requestedIds.indexOf(b.id));
      setUsers(selected);
      if (selected.length === 0) {
        setError('None of the requested users were found.');
      }
    } catch (err) {
      console.error('Failed to load users for badges:', err);
      setError('Unable to load users. Verify your access and try again.');
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, [requestedIds]);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  useEffect(() => {
    const generate = async () => {
      try {
        const entries = await Promise.all(
          users.map(async (u) => {
            // Payload is the stored employee_id verbatim — what employee-login
            // and scanner/resolve-action expect.
            const dataUrl = await QRCode.toDataURL(u.employee_id, { width: 240, margin: 1 });
            return [u.id, dataUrl] as const;
          })
        );
        setQrDataUrls(Object.fromEntries(entries));
      } catch (err) {
        console.error('Failed to generate badge QR codes:', err);
      }
    };
    if (users.length > 0) generate();
  }, [users]);

  if (loading) {
    return <div className="p-8">Loading...</div>;
  }

  if (error) {
    return (
      <div className="p-8 max-w-3xl mx-auto">
        <ErrorState
          title={loadFailed ? "Couldn't load badges" : 'No badges to print'}
          message={error}
          onRetry={loadFailed ? loadUsers : undefined}
        />
        <div className="mt-6">
          <button onClick={() => window.close()} className="btn-secondary">
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="p-8 print:p-0 bg-white text-black min-h-screen">
      <style>{`
        .badge-card {
          /* CR80 card: must hold exact physical size — never shrink as a flex item. */
          width: 3.375in;
          height: 2.125in;
          flex-shrink: 0;
          border: 1px solid #9ca3af;
          break-inside: avoid;
          page-break-inside: avoid;
        }
        @media print {
          @page { size: letter; margin: 0.5in; }
          body { -webkit-print-color-adjust: exact; }
          .no-print { display: none !important; }
          .badge-card { border: 1px dashed #9ca3af; } /* cut line */
        }
      `}</style>

      <div className="no-print mb-6 flex items-center justify-between max-w-3xl">
        <div>
          <h1 className="text-xl font-bold">Employee Badges</h1>
          <p className="text-sm text-gray-600">
            {users.length} badge{users.length === 1 ? '' : 's'} — CR80 card size (3.375in x 2.125in), cut along the
            dashed lines.
          </p>
        </div>
        <div className="flex gap-3">
          <button onClick={() => window.print()} className="btn-primary">
            Print Badges
          </button>
          <button onClick={() => window.close()} className="btn-secondary">
            Close
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-4 print:gap-2">
        {users.map((u) => (
          <div key={u.id} className="badge-card bg-white flex overflow-hidden" data-testid={`badge-${u.id}`}>
            {/* Brand stripe */}
            <div className="w-2 shrink-0" style={{ backgroundColor: '#1B4D9C' }} />
            <div className="flex-1 flex items-center justify-between px-3 py-2 min-w-0">
              <div className="min-w-0 pr-2">
                <div className="text-[10px] font-semibold tracking-widest" style={{ color: '#1B4D9C' }}>
                  WERCO
                </div>
                <div className="text-base font-bold leading-tight truncate">
                  {u.first_name} {u.last_name}
                </div>
                <div className="text-xs text-gray-700">{roleLabels[u.role] || u.role}</div>
                {u.department && <div className="text-xs text-gray-500 truncate">{u.department}</div>}
                <div className="text-[10px] font-mono text-gray-600 mt-1">ID: {u.employee_id}</div>
                {!u.is_active && <div className="text-[10px] font-bold text-red-600">INACTIVE</div>}
              </div>
              <div className="shrink-0 text-center">
                {qrDataUrls[u.id] && (
                  <img
                    src={qrDataUrls[u.id]}
                    alt={`Badge code for ${u.employee_id}`}
                    style={{ width: '1.4in', height: '1.4in' }}
                  />
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
