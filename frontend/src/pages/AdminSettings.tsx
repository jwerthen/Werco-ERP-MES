import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import type { UserRole } from '../types';
import { formatCentralDateTime } from '../utils/centralTime';
import {
  Cog6ToothIcon,
  PlusIcon,
  PencilIcon,
  TrashIcon,
  XMarkIcon,
  CheckIcon,
  ArrowPathIcon,
  BuildingOfficeIcon,
  ClockIcon,
  DocumentTextIcon,
  ShieldCheckIcon,
  UsersIcon,
  GlobeAltIcon,
  CpuChipIcon,
  TvIcon,
  PrinterIcon,
  ChatBubbleLeftRightIcon,
  EnvelopeIcon,
} from '@heroicons/react/24/outline';
import CarrierIntegrationsTab from '../components/admin/CarrierIntegrationsTab';
import PrintIntegrationsTab from '../components/admin/PrintIntegrationsTab';
import AIUsageTab from '../components/admin/AIUsageTab';
import RuntimeMetricsTab from '../components/admin/RuntimeMetricsTab';
import AIEgressTab from '../components/admin/AIEgressTab';
import SmsEgressTab from '../components/admin/SmsEgressTab';
import EmailRecipientsTab from '../components/admin/EmailRecipientsTab';
import DisplayTokensTab from '../components/admin/DisplayTokensTab';
import { ConfirmDialog, EmptyState, ErrorState, FormField, useToast } from '../components/ui';

type TabKey = 'workcenters' | 'workcentertypes' | 'employees' | 'roles' | 'carriers' | 'printing' | 'performance' | 'aiusage' | 'aiprivacy' | 'smsprivacy' | 'emails' | 'displays' | 'audit';


const tabs: { key: TabKey; label: string; icon: React.ComponentType<any> }[] = [
  { key: 'workcenters', label: 'Work Center Rates', icon: BuildingOfficeIcon },
  { key: 'workcentertypes', label: 'Work Center Types', icon: BuildingOfficeIcon },
  { key: 'employees', label: 'Employees', icon: UsersIcon },
  { key: 'emails', label: 'Email Recipients', icon: EnvelopeIcon },
  { key: 'roles', label: 'Roles & Permissions', icon: ShieldCheckIcon },
  { key: 'carriers', label: 'Carriers / Integrations', icon: GlobeAltIcon },
  { key: 'printing', label: 'Label Printing', icon: PrinterIcon },
  { key: 'performance', label: 'App Performance', icon: ClockIcon },
  { key: 'aiusage', label: 'AI Usage & Cost', icon: CpuChipIcon },
  { key: 'aiprivacy', label: 'AI Privacy', icon: ShieldCheckIcon },
  { key: 'smsprivacy', label: 'SMS Privacy', icon: ChatBubbleLeftRightIcon },
  { key: 'displays', label: 'Wallboard Displays', icon: TvIcon },
  { key: 'audit', label: 'Audit Log', icon: ClockIcon },
];

const EMPLOYEE_ID_PATTERN = /^\d{4}$/;

const generateEmployeePassword = () => {
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
  const lower = 'abcdefghijkmnpqrstuvwxyz';
  const numbers = '23456789';
  const specials = '!@#$%^&*-_+=';
  const all = `${upper}${lower}${numbers}${specials}`;
  const pick = (chars: string) => chars[Math.floor(Math.random() * chars.length)];
  const base = [
    pick(upper),
    pick(lower),
    pick(numbers),
    pick(specials),
  ];
  while (base.length < 14) {
    base.push(pick(all));
  }
  for (let i = base.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [base[i], base[j]] = [base[j], base[i]];
  }
  return base.join('');
};

const normalizeEmployeeId = (value: string) => value.replace(/\D/g, '').slice(0, 4);

const padEmployeeId = (value: string) => {
  const digits = normalizeEmployeeId(value);
  if (digits.length === 0) return '';
  return digits.padStart(4, '0');
};

const buildEmployeeEmail = (employeeId: string) => `employee-${employeeId}@werco.com`;

export default function AdminSettings() {
  const [searchParams] = useSearchParams();
  // Honor a ?tab=<key> deep link (e.g. the "enable carrier egress" CTA in the
  // Schedule-Shipment wizard links to /admin/settings?tab=carriers).
  const initialTab = ((): TabKey => {
    const requested = searchParams.get('tab');
    return tabs.some((t) => t.key === requested) ? (requested as TabKey) : 'workcenters';
  })();
  const { showToast } = useToast();
  const [activeTab, setActiveTab] = useState<TabKey>(initialTab);
  const tabNavigationRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const revealActiveTab = () => {
      const navigation = tabNavigationRef.current;
      const active = navigation?.querySelector('[aria-current="page"]');
      if (!navigation || !active) return;
      const bounds = navigation.getBoundingClientRect();
      const selected = active.getBoundingClientRect();
      if (selected.right > bounds.right) navigation.scrollLeft += selected.right - bounds.right;
      else if (selected.left < bounds.left) navigation.scrollLeft += selected.left - bounds.left;
    };
    revealActiveTab();
    window.addEventListener('resize', revealActiveTab);
    return () => window.removeEventListener('resize', revealActiveTab);
  }, [activeTab]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [showInactive, setShowInactive] = useState(false);

  // Data states
  const [workCenterRates, setWorkCenterRates] = useState<any[]>([]);
  const [workCenterTypes, setWorkCenterTypes] = useState<string[]>([]);
  const [workCenterTypesInUse, setWorkCenterTypesInUse] = useState<string[]>([]);
  const [employees, setEmployees] = useState<EmployeeUser[]>([]);
  const [rolePermissions, setRolePermissions] = useState<{
    role_permissions: Record<string, string[]>;
    all_permissions: string[];
    permission_categories: Record<string, string[]>;
    roles: { value: string; label: string }[];
  } | null>(null);
  const [auditLog, setAuditLog] = useState<any[]>([]);
  const [workCenterTypeInput, setWorkCenterTypeInput] = useState('');

  // Modal states
  const [editModal, setEditModal] = useState<{ type: string; item: any } | null>(null);
  const [employeeModalOpen, setEmployeeModalOpen] = useState(false);
  const [editingEmployee, setEditingEmployee] = useState<EmployeeUser | null>(null);

  const loadTabData = useCallback(async (tab: TabKey) => {
    setLoading(true);
    setLoadError(false);
    try {
      switch (tab) {
        case 'workcenters':
          setWorkCenterRates(await api.getAdminWorkCenterRates(showInactive));
          break;
        case 'workcentertypes': {
          const response = await api.getAdminWorkCenterTypes();
          setWorkCenterTypes(response?.types || []);
          setWorkCenterTypesInUse(response?.in_use || []);
          break;
        }
        case 'employees': {
          const userList = await api.getUsers(showInactive);
          const employeeUsers = (userList || []).filter((user: EmployeeUser) => EMPLOYEE_ID_PATTERN.test(user.employee_id));
          setEmployees(employeeUsers);
          break;
        }
        case 'roles':
          setRolePermissions(await api.getRolePermissions());
          break;
        case 'audit':
          setAuditLog(await api.getSettingsAuditLog());
          break;
      }
    } catch (err) {
      console.error('Failed to load data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  useEffect(() => {
    loadTabData(activeTab);
  }, [activeTab, loadTabData]);

  const handleSave = async (data: any) => {
    try {
      await api.updateAdminWorkCenterRate(data.id, { hourly_rate: data.hourly_rate });
      setEditModal(null);
      loadTabData(activeTab);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save');
    }
  };

  const normalizeWorkCenterType = (value: string) => {
    const trimmed = value.trim().toLowerCase();
    return trimmed
      .replace(/[^a-z0-9\s_-]/g, '')
      .replace(/[\s-]+/g, '_')
      .replace(/^_+|_+$/g, '');
  };

  const WORK_CENTER_TYPE_ACRONYMS = new Set(['cnc', 'cmm', 'edm', 'tig', 'mig', 'qa', 'qc', 'nc']);
  const formatWorkCenterTypeLabel = (value: string) =>
    value
      .split('_')
      .filter(Boolean)
      .map((word) =>
        WORK_CENTER_TYPE_ACRONYMS.has(word.toLowerCase())
          ? word.toUpperCase()
          : word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
      )
      .join(' ');

  const saveWorkCenterTypes = async (nextTypes: string[]) => {
    try {
      const response = await api.updateAdminWorkCenterTypes(nextTypes);
      setWorkCenterTypes(response?.types || nextTypes);
      setWorkCenterTypesInUse(response?.in_use || workCenterTypesInUse);
      setWorkCenterTypeInput('');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to update work center types');
    }
  };

  const handleEmployeeSave = async (form: EmployeeFormValues, existing?: EmployeeUser | null) => {
    try {
      if (existing) {
        await api.updateUser(existing.id, {
          first_name: form.first_name,
          last_name: form.last_name,
          department: form.department || null,
        });
      } else {
        const employeeId = normalizeEmployeeId(form.employee_id);
        const password = generateEmployeePassword();
        await api.createUser({
          email: buildEmployeeEmail(employeeId),
          employee_id: employeeId,
          first_name: form.first_name,
          last_name: form.last_name,
          password,
          role: 'operator',
          department: form.department || null,
        });
      }
      setEmployeeModalOpen(false);
      setEditingEmployee(null);
      loadTabData('employees');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save employee');
    }
  };

  const handleEmployeeToggleActive = async (employee: EmployeeUser) => {
    try {
      if (employee.is_active) {
        await api.deactivateUser(employee.id);
      } else {
        await api.activateUser(employee.id);
      }
      loadTabData('employees');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to update employee status');
    }
  };

  const filteredEmployees = useMemo(() => {
    return employees;
  }, [employees]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title flex items-center gap-3">
            <Cog6ToothIcon className="h-8 w-8 text-werco-600" />
            Admin Settings
          </h1>
          <p className="page-subtitle">Manage work centers, people and system configuration</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-surface-200">
        <nav ref={tabNavigationRef} aria-label="Settings sections" className="flex gap-1 overflow-x-auto pb-px">
          {tabs.map(tab => (
            <button
              key={tab.key}
              aria-current={activeTab === tab.key ? 'page' : undefined}
              onClick={() => setActiveTab(tab.key)}
              className={`
                flex items-center gap-2 px-4 py-3 text-sm font-medium whitespace-nowrap border-b-2 transition-colors
                ${activeTab === tab.key
                  ? 'border-werco-600 text-werco-600'
                  : 'border-transparent text-surface-500 hover:text-surface-700 hover:border-surface-300'
                }
              `}
            >
              <tab.icon className="h-5 w-5" />
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab content */}
      <div className="card">
        {/* Show inactive toggle (not for configuration-only tabs) */}
        {['workcenters', 'employees'].includes(activeTab) && (
          <div className="flex items-center justify-between mb-4 pb-4 border-b border-surface-200">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={showInactive}
                onChange={(e) => setShowInactive(e.target.checked)}
                className="checkbox"
                aria-label="Show inactive"
              />
              <span className="text-sm text-surface-600">Show inactive</span>
            </label>
            {activeTab === 'employees' && (
              <button
                onClick={() => { setEditingEmployee(null); setEmployeeModalOpen(true); }}
                className="btn-primary btn-sm"
              >
                <PlusIcon className="h-4 w-4 mr-1" />
                Add Employee
              </button>
            )}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <div className="spinner h-8 w-8"></div>
          </div>
        ) : loadError && !['carriers', 'printing', 'performance', 'aiusage', 'aiprivacy', 'smsprivacy', 'displays'].includes(activeTab) ? (
          <ErrorState
            message="Could not load this settings tab."
            onRetry={() => loadTabData(activeTab)}
          />
        ) : (
          <>
            {activeTab === 'workcenters' && <WorkCenterRatesTable data={workCenterRates} onEdit={(item) => setEditModal({ type: 'workcenter', item })} />}
            {activeTab === 'workcentertypes' && (
              <WorkCenterTypesPanel
                types={workCenterTypes}
                inUse={workCenterTypesInUse}
                inputValue={workCenterTypeInput}
                onInputChange={setWorkCenterTypeInput}
                onSave={saveWorkCenterTypes}
                normalizeType={normalizeWorkCenterType}
                formatLabel={formatWorkCenterTypeLabel}
              />
            )}
            {activeTab === 'employees' && (
              <EmployeesTable
                data={filteredEmployees}
                onEdit={(employee) => { setEditingEmployee(employee); setEmployeeModalOpen(true); }}
                onToggleActive={handleEmployeeToggleActive}
                onAdd={() => { setEditingEmployee(null); setEmployeeModalOpen(true); }}
              />
            )}
            {activeTab === 'roles' && rolePermissions && <RolePermissionsManager data={rolePermissions} onUpdate={() => loadTabData('roles')} />}
            {activeTab === 'carriers' && <CarrierIntegrationsTab />}
            {activeTab === 'printing' && <PrintIntegrationsTab />}
            {activeTab === 'performance' && <RuntimeMetricsTab />}
            {activeTab === 'aiusage' && <AIUsageTab />}
            {activeTab === 'aiprivacy' && <AIEgressTab />}
            {activeTab === 'smsprivacy' && <SmsEgressTab />}
            {activeTab === 'emails' && <EmailRecipientsTab />}
            {activeTab === 'displays' && <DisplayTokensTab />}
            {activeTab === 'audit' && <AuditLogTable data={auditLog} />}
          </>
        )}
      </div>

      {/* Edit Modal */}
      {editModal && (
        <EditModal
          type={editModal.type}
          item={editModal.item}
          onSave={handleSave}
          onClose={() => setEditModal(null)}
        />
      )}

      {employeeModalOpen && (
        <EmployeeModal
          employee={editingEmployee}
          onSave={(form) => handleEmployeeSave(form, editingEmployee)}
          onClose={() => { setEmployeeModalOpen(false); setEditingEmployee(null); }}
        />
      )}
    </div>
  );
}

// ============ TABLE COMPONENTS ============

function WorkCenterRatesTable({ data, onEdit }: { data: any[]; onEdit: (item: any) => void }) {
  if (data.length === 0) return <EmptyState icon={BuildingOfficeIcon} title="No work centers found" description="Work centers created on the Work Centers page will appear here for rate editing." />;
  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Code</th>
            <th>Name</th>
            <th>Type</th>
            <th>Hourly Rate</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map(wc => (
            <tr key={wc.id} className={!wc.is_active ? 'opacity-50' : ''}>
              <td className="font-mono font-medium">{wc.code}</td>
              <td className="font-medium">{wc.name}</td>
              <td><span className="badge badge-neutral">{wc.work_center_type?.replace(/_/g, ' ')}</span></td>
              <td className="tabular-nums text-lg font-semibold text-werco-600">${wc.hourly_rate?.toFixed(2)}</td>
              <td><StatusBadge active={wc.is_active} /></td>
              <td>
                <button onClick={() => onEdit(wc)} className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-500/10">
                  <PencilIcon className="h-4 w-4" />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WorkCenterTypesPanel({
  types,
  inUse,
  inputValue,
  onInputChange,
  onSave,
  normalizeType,
  formatLabel,
}: {
  types: string[];
  inUse: string[];
  inputValue: string;
  onInputChange: (value: string) => void;
  onSave: (types: string[]) => void;
  normalizeType: (value: string) => string;
  formatLabel: (value: string) => string;
}) {
  const normalizedInput = normalizeType(inputValue);
  const canAdd = normalizedInput.length > 0 && !types.includes(normalizedInput);
  const lockedTypes = new Set(inUse || []);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-6">
        <div>
          <h3 className="text-lg font-semibold text-surface-900">Work Center Types</h3>
          <p className="text-sm text-surface-600">
            These types power the Work Center dropdowns and the grouping on the Work Centers page.
          </p>
        </div>
        <div className="text-xs text-surface-500 bg-surface-100 px-3 py-2 rounded-lg">
          Types are normalized (spaces → underscores, lowercase).
        </div>
      </div>

      <div className="flex flex-col sm:flex-row gap-3">
        <div className="flex-1">
          <label htmlFor="wct-add-type" className="label">Add type</label>
          <input
            id="wct-add-type"
            type="text"
            value={inputValue}
            onChange={(e) => onInputChange(e.target.value)}
            className="input"
            placeholder="e.g., Blending or Final Assembly"
            aria-label="Add type"
          />
          {inputValue && (
            <p className="text-xs text-surface-500 mt-1">
              Saved as: <span className="font-mono">{normalizedInput || '—'}</span>
            </p>
          )}
        </div>
        <div className="flex items-end">
          <button
            type="button"
            onClick={() => canAdd && onSave([...types, normalizedInput])}
            className="btn-primary"
            disabled={!canAdd}
          >
            <PlusIcon className="h-4 w-4 mr-2" />
            Add Type
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {types.length === 0 && (
          <div className="md:col-span-2 lg:col-span-3">
            <EmptyState
              icon={BuildingOfficeIcon}
              title="No work center types configured yet"
              description="Add a type above to populate the Work Center dropdowns."
            />
          </div>
        )}
        {types.map((type) => (
          <div key={type} className="flex items-center justify-between border border-surface-200 rounded-lg px-3 py-2 bg-fd-panel">
            <div>
              <div className="text-sm font-medium text-surface-900">{formatLabel(type)}</div>
              <div className="text-xs text-surface-500 font-mono">{type}</div>
              {lockedTypes.has(type) && (
                <div className="text-xs text-amber-600 mt-1">In use by existing work centers</div>
              )}
            </div>
            <button
              type="button"
              onClick={() => !lockedTypes.has(type) && onSave(types.filter((t) => t !== type))}
              className={`text-surface-400 ${lockedTypes.has(type) ? 'cursor-not-allowed opacity-50' : 'hover:text-red-600'}`}
              title="Remove type"
              disabled={lockedTypes.has(type)}
            >
              <TrashIcon className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function AuditLogTable({ data }: { data: any[] }) {
  if (data.length === 0) return <EmptyState icon={DocumentTextIcon} title="No audit entries" description="Changes to settings will be recorded here." />;
  return (
    <div className="table-container border-0">
      <table className="table table-compact">
        <thead>
          <tr>
            <th>Date/Time</th>
            <th>User</th>
            <th>Entity</th>
            <th>Action</th>
            <th>Field</th>
            <th>Old Value</th>
            <th>New Value</th>
          </tr>
        </thead>
        <tbody>
          {data.map(entry => (
            <tr key={entry.id}>
              <td className="text-sm tabular-nums">{formatCentralDateTime(entry.changed_at)}</td>
              <td className="font-medium">{entry.user_name || '—'}</td>
              <td>
                <span className="badge badge-neutral">{entry.entity_type}</span>
                <span className="ml-1 text-surface-500">{entry.entity_name}</span>
              </td>
              <td>
                <span className={`badge ${entry.action === 'create' ? 'badge-success' : entry.action === 'delete' ? 'badge-danger' : 'badge-warning'}`}>
                  {entry.action}
                </span>
              </td>
              <td className="font-mono text-xs">{entry.field_changed || '—'}</td>
              <td className="text-sm text-surface-500 max-w-[150px] truncate">{entry.old_value || '—'}</td>
              <td className="text-sm max-w-[150px] truncate">{entry.new_value || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ============ EMPLOYEES ============

interface EmployeeUser {
  id: number;
  employee_id: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  department?: string;
  email: string;
  is_active: boolean;
  created_at: string;
}

interface EmployeeFormValues {
  employee_id: string;
  first_name: string;
  last_name: string;
  department?: string;
}

function EmployeesTable({
  data,
  onEdit,
  onToggleActive,
  onAdd,
}: {
  data: EmployeeUser[];
  onEdit: (employee: EmployeeUser) => void;
  onToggleActive: (employee: EmployeeUser) => void;
  onAdd: () => void;
}) {
  if (data.length === 0) {
    return (
      <EmptyState
        icon={UsersIcon}
        title="No employees configured yet"
        description="Add operator accounts tied to a 4-digit ID for kiosk sign-in."
        action={{ label: 'Add Employee', onClick: onAdd }}
      />
    );
  }

  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Employee</th>
            <th>Employee ID</th>
            <th>Department</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map((employee) => (
            <tr key={employee.id} className={!employee.is_active ? 'opacity-50' : ''}>
              <td className="font-medium">{employee.first_name} {employee.last_name}</td>
              <td className="font-mono text-sm">{employee.employee_id}</td>
              <td className="text-sm text-surface-600">{employee.department || 'â€”'}</td>
              <td><StatusBadge active={employee.is_active} /></td>
              <td className="flex items-center gap-1">
                <button
                  onClick={() => onEdit(employee)}
                  className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-500/10"
                  title="Edit"
                >
                  <PencilIcon className="h-4 w-4" />
                </button>
                <button
                  onClick={() => onToggleActive(employee)}
                  className={`p-2 rounded-lg ${employee.is_active ? 'text-surface-500 hover:text-red-600 hover:bg-red-500/10' : 'text-surface-500 hover:text-werco-600 hover:bg-werco-500/10'}`}
                  title={employee.is_active ? 'Deactivate' : 'Activate'}
                >
                  {employee.is_active ? <TrashIcon className="h-4 w-4" /> : <CheckIcon className="h-4 w-4" />}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EmployeeModal({
  employee,
  onSave,
  onClose,
}: {
  employee: EmployeeUser | null;
  onSave: (form: EmployeeFormValues) => void;
  onClose: () => void;
}) {
  const [form, setForm] = useState<EmployeeFormValues>({
    employee_id: employee?.employee_id || '',
    first_name: employee?.first_name || '',
    last_name: employee?.last_name || '',
    department: employee?.department || '',
  });
  const [error, setError] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const normalizedId = padEmployeeId(form.employee_id);
    if (!employee && !EMPLOYEE_ID_PATTERN.test(normalizedId)) {
      setError('Employee ID must be exactly 4 digits.');
      return;
    }
    setError('');
    onSave({ ...form, employee_id: normalizedId });
  };

  return (
    <div
      className="modal-overlay"
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="modal max-w-md">
        <div className="modal-header">
          <h3 className="text-lg font-semibold">
            {employee ? 'Edit Employee' : 'Add Employee'}
          </h3>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-surface-100">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="modal-body space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <FormField label="First Name">
                {(field) => (
                  <input
                    {...field}
                    className="input"
                    value={form.first_name}
                    onChange={(e) => setForm({ ...form, first_name: e.target.value })}
                    required
                  />
                )}
              </FormField>
              <FormField label="Last Name">
                {(field) => (
                  <input
                    {...field}
                    className="input"
                    value={form.last_name}
                    onChange={(e) => setForm({ ...form, last_name: e.target.value })}
                    required
                  />
                )}
              </FormField>
            </div>
            <FormField label="Employee ID (4 digits)">
              {(field) => (
                <input
                  {...field}
                  className="input font-mono tracking-widest text-center"
                  value={form.employee_id}
                  onChange={(e) => setForm({ ...form, employee_id: normalizeEmployeeId(e.target.value) })}
                  onBlur={() => setForm({ ...form, employee_id: padEmployeeId(form.employee_id) })}
                  placeholder="0000"
                  maxLength={4}
                  inputMode="numeric"
                  disabled={!!employee}
                  required
                />
              )}
            </FormField>
            <FormField label="Department (optional)">
              {(field) => (
                <input
                  {...field}
                  className="input"
                  value={form.department || ''}
                  onChange={(e) => setForm({ ...form, department: e.target.value })}
                />
              )}
            </FormField>
            {!employee && (
              <div className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600">
                This creates an operator account tied to the 4-digit ID. Kiosk sign-ins will use this ID and show the
                employee name. Short IDs are left-padded with zeros (e.g., 7 â†’ 0007).
              </div>
            )}
            {error && <div className="text-sm text-red-600">{error}</div>}
          </div>
          <div className="modal-footer">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" className="btn-primary">Save</button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ============ ROLE PERMISSIONS MANAGER ============

interface RolePermissionsData {
  role_permissions: Record<string, string[]>;
  all_permissions: string[];
  permission_categories: Record<string, string[]>;
  roles: { value: string; label: string }[];
}

function RolePermissionsManager({ data, onUpdate }: { data: RolePermissionsData; onUpdate: () => void }) {
  const { showToast } = useToast();
  const [selectedRole, setSelectedRole] = useState<string>(data.roles[0]?.value || 'admin');
  const [permissions, setPermissions] = useState<string[]>(data.role_permissions[selectedRole] || []);
  const [saving, setSaving] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);

  useEffect(() => {
    setPermissions(data.role_permissions[selectedRole] || []);
    setHasChanges(false);
  }, [selectedRole, data]);

  const togglePermission = (permission: string) => {
    setPermissions(prev => {
      const newPerms = prev.includes(permission)
        ? prev.filter(p => p !== permission)
        : [...prev, permission];
      setHasChanges(true);
      return newPerms;
    });
  };

  const toggleCategory = (category: string) => {
    const categoryPerms = data.permission_categories[category];
    const allSelected = categoryPerms.every(p => permissions.includes(p));
    
    setPermissions(prev => {
      const newPerms = allSelected
        ? prev.filter(p => !categoryPerms.includes(p))
        : Array.from(new Set([...prev, ...categoryPerms]));
      setHasChanges(true);
      return newPerms;
    });
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.updateRolePermissions(selectedRole, permissions);
      setHasChanges(false);
      onUpdate();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save permissions');
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    if (saving) return;
    setResetConfirmOpen(true);
  };

  const handleConfirmReset = async () => {
    if (saving) return;
    setSaving(true);
    try {
      await api.resetRolePermissions(selectedRole);
      onUpdate();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to reset permissions');
    } finally {
      setSaving(false);
      setResetConfirmOpen(false);
    }
  };

  const formatPermissionLabel = (permission: string) => {
    const [, action] = permission.split(':');
    return action.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
  };

  return (
    <div className="space-y-6">
      {/* Role selector and actions */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <label htmlFor="role-perm-select" className="text-sm font-medium text-surface-700">Select Role:</label>
          <select
            id="role-perm-select"
            value={selectedRole}
            onChange={(e) => setSelectedRole(e.target.value)}
            className="input w-48"
          >
            {data.roles.map(role => (
              <option key={role.value} value={role.value}>
                {role.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleReset}
            disabled={saving}
            className="btn-secondary btn-sm"
          >
            <ArrowPathIcon className="h-4 w-4 mr-1" />
            Reset to Default
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !hasChanges}
            className="btn-primary btn-sm"
          >
            {saving ? (
              <span className="spinner h-4 w-4 mr-1" />
            ) : (
              <CheckIcon className="h-4 w-4 mr-1" />
            )}
            Save Changes
          </button>
        </div>
      </div>

      {hasChanges && (
        <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg px-4 py-2 text-sm text-amber-300">
          You have unsaved changes. Click "Save Changes" to apply them.
        </div>
      )}

      {/* Permission categories */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {Object.entries(data.permission_categories).map(([category, categoryPerms]) => {
          const selectedCount = categoryPerms.filter(p => permissions.includes(p)).length;
          const allSelected = selectedCount === categoryPerms.length;
          const someSelected = selectedCount > 0 && !allSelected;

          return (
            <div key={category} className="bg-surface-50 rounded-xl p-4">
              <div className="flex items-center justify-between mb-3 pb-2 border-b border-surface-200">
                <h4 className="font-medium text-surface-900">{category}</h4>
                <button
                  onClick={() => toggleCategory(category)}
                  className={`text-xs px-2 py-1 rounded ${
                    allSelected
                      ? 'bg-werco-100 text-werco-700'
                      : someSelected
                      ? 'bg-amber-500/20 text-amber-400'
                      : 'bg-surface-200 text-surface-600'
                  }`}
                >
                  {selectedCount}/{categoryPerms.length}
                </button>
              </div>
              <div className="space-y-2">
                {categoryPerms.map(permission => (
                  <label
                    key={permission}
                    className="flex items-center gap-2 cursor-pointer hover:bg-surface-100 rounded px-2 py-1 -mx-2"
                  >
                    <input
                      type="checkbox"
                      checked={permissions.includes(permission)}
                      onChange={() => togglePermission(permission)}
                      className="checkbox"
                      aria-label={formatPermissionLabel(permission)}
                    />
                    <span className="text-sm text-surface-700">
                      {formatPermissionLabel(permission)}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {/* Permission summary */}
      <div className="bg-surface-100 rounded-xl p-4">
        <h4 className="font-medium text-surface-900 mb-2">
          {selectedRole.charAt(0).toUpperCase() + selectedRole.slice(1)} has {permissions.length} of {data.all_permissions.length} permissions
        </h4>
        <div className="flex flex-wrap gap-1">
          {permissions.map(p => (
            <span key={p} className="badge badge-sm bg-werco-100 text-werco-700">
              {p}
            </span>
          ))}
        </div>
      </div>

      {/* Reset-to-defaults confirm (discards any customized permission set) */}
      <ConfirmDialog
        open={resetConfirmOpen}
        title="Reset Permissions"
        message={`Reset ${selectedRole} permissions to defaults?`}
        confirmLabel="Reset"
        pending={saving}
        variant="warning"
        onConfirm={handleConfirmReset}
        onCancel={() => {
          if (!saving) setResetConfirmOpen(false);
        }}
      />
    </div>
  );
}

// ============ HELPER COMPONENTS ============

function StatusBadge({ active }: { active: boolean }) {
  return active ? (
    <span className="badge badge-success">Active</span>
  ) : (
    <span className="badge badge-neutral">Inactive</span>
  );
}

// ============ EDIT MODAL ============

function EditModal({ type, item, onSave, onClose }: { type: string; item: any; onSave: (data: any) => void; onClose: () => void }) {
  const [form, setForm] = useState(item || getDefaultForm(type));

  const update = (field: string, value: any) => setForm({ ...form, [field]: value });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave(form);
  };

  return (
    <div
      className="modal-overlay"
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="modal max-w-lg">
        <div className="modal-header">
          <h3 className="text-lg font-semibold">{item ? 'Edit' : 'Add'} {type.charAt(0).toUpperCase() + type.slice(1)}</h3>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-surface-100">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="modal-body space-y-4">
            {type === 'workcenter' && <WorkCenterForm form={form} update={update} />}
          </div>
          <div className="modal-footer">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" className="btn-primary">Save</button>
          </div>
        </form>
      </div>
    </div>
  );
}

function getDefaultForm(type: string): any {
  switch (type) {
    case 'workcenter': return { hourly_rate: 0 };
    default: return {};
  }
}

function WorkCenterForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <FormField label="Hourly Rate ($)">
      {(field) => (
        <input {...field} type="number" step="0.01" className="input" value={form.hourly_rate} onChange={e => update('hourly_rate', parseFloat(e.target.value) || 0)} required />
      )}
    </FormField>
  );
}
