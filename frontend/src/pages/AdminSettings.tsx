import React, { useEffect, useState, useCallback, useMemo } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import type { UserRole } from '../types';
import {
  Cog6ToothIcon,
  PlusIcon,
  PencilIcon,
  TrashIcon,
  XMarkIcon,
  CheckIcon,
  ArrowPathIcon,
  CubeIcon,
  WrenchScrewdriverIcon,
  SparklesIcon,
  CurrencyDollarIcon,
  BuildingOfficeIcon,
  TruckIcon,
  ClockIcon,
  DocumentTextIcon,
  ShieldCheckIcon,
  UsersIcon,
} from '@heroicons/react/24/outline';

type TabKey = 'materials' | 'machines' | 'finishes' | 'labor' | 'workcenters' | 'services' | 'overhead' | 'employees' | 'roles' | 'audit';

const MATERIAL_CATEGORIES = ['steel', 'stainless', 'aluminum', 'brass', 'copper', 'titanium', 'plastic', 'other'];
const MACHINE_TYPES = ['cnc_mill_3axis', 'cnc_mill_4axis', 'cnc_mill_5axis', 'cnc_lathe', 'laser_fiber', 'laser_co2', 'plasma', 'waterjet', 'press_brake', 'punch_press'];
const PROCESS_TYPES = ['heat_treat', 'plating', 'coating', 'machining', 'testing', 'welding', 'assembly', 'inspection', 'other'];
const COST_UNITS = ['per_part', 'per_lb', 'per_sqft', 'per_hour', 'flat_rate'];

const tabs: { key: TabKey; label: string; icon: React.ComponentType<any> }[] = [
  { key: 'materials', label: 'Materials', icon: CubeIcon },
  { key: 'machines', label: 'Machines', icon: WrenchScrewdriverIcon },
  { key: 'finishes', label: 'Finishes', icon: SparklesIcon },
  { key: 'labor', label: 'Labor Rates', icon: CurrencyDollarIcon },
  { key: 'workcenters', label: 'Work Center Rates', icon: BuildingOfficeIcon },
  { key: 'services', label: 'Outside Services', icon: TruckIcon },
  { key: 'overhead', label: 'Overhead/Markup', icon: Cog6ToothIcon },
  { key: 'employees', label: 'Employees', icon: UsersIcon },
  { key: 'roles', label: 'Roles & Permissions', icon: ShieldCheckIcon },
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

const normalizeEmployeeId = (value: string) => {
  const digits = value.replace(/\D/g, '').slice(0, 4);
  if (digits.length === 0) return '';
  return digits.padStart(4, '0');
};

const buildEmployeeEmail = (employeeId: string) => `employee-${employeeId}@werco.local`;

export default function AdminSettings() {
  const [activeTab, setActiveTab] = useState<TabKey>('materials');
  const [loading, setLoading] = useState(false);
  const [showInactive, setShowInactive] = useState(false);

  // Data states
  const [materials, setMaterials] = useState<any[]>([]);
  const [machines, setMachines] = useState<any[]>([]);
  const [finishes, setFinishes] = useState<any[]>([]);
  const [laborRates, setLaborRates] = useState<any[]>([]);
  const [workCenterRates, setWorkCenterRates] = useState<any[]>([]);
  const [outsideServices, setOutsideServices] = useState<any[]>([]);
  const [overhead, setOverhead] = useState<Record<string, any>>({});
  const [employees, setEmployees] = useState<EmployeeUser[]>([]);
  const [rolePermissions, setRolePermissions] = useState<{
    role_permissions: Record<string, string[]>;
    all_permissions: string[];
    permission_categories: Record<string, string[]>;
    roles: { value: string; label: string }[];
  } | null>(null);
  const [auditLog, setAuditLog] = useState<any[]>([]);

  // Modal states
  const [editModal, setEditModal] = useState<{ type: string; item: any } | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<{ type: string; item: any } | null>(null);
  const [employeeModalOpen, setEmployeeModalOpen] = useState(false);
  const [editingEmployee, setEditingEmployee] = useState<EmployeeUser | null>(null);

  const loadTabData = useCallback(async (tab: TabKey) => {
    setLoading(true);
    try {
      switch (tab) {
        case 'materials':
          setMaterials(await api.getAdminMaterials(showInactive));
          break;
        case 'machines':
          setMachines(await api.getAdminMachines(showInactive));
          break;
        case 'finishes':
          setFinishes(await api.getAdminFinishes(showInactive));
          break;
        case 'labor':
          setLaborRates(await api.getAdminLaborRates(showInactive));
          break;
        case 'workcenters':
          setWorkCenterRates(await api.getAdminWorkCenterRates(showInactive));
          break;
        case 'services':
          setOutsideServices(await api.getAdminOutsideServices(showInactive));
          break;
        case 'overhead':
          setOverhead(await api.getAdminOverhead());
          break;
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
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  useEffect(() => {
    loadTabData(activeTab);
  }, [activeTab, loadTabData]);

  const handleSave = async (type: string, data: any) => {
    try {
      if (data.id) {
        // Update
        switch (type) {
          case 'material': await api.updateAdminMaterial(data.id, data); break;
          case 'machine': await api.updateAdminMachine(data.id, data); break;
          case 'finish': await api.updateAdminFinish(data.id, data); break;
          case 'labor': await api.updateAdminLaborRate(data.id, data); break;
          case 'workcenter': await api.updateAdminWorkCenterRate(data.id, data); break;
          case 'service': await api.updateAdminOutsideService(data.id, data); break;
        }
      } else {
        // Create
        switch (type) {
          case 'material': await api.createAdminMaterial(data); break;
          case 'machine': await api.createAdminMachine(data); break;
          case 'finish': await api.createAdminFinish(data); break;
          case 'labor': await api.createAdminLaborRate(data); break;
          case 'service': await api.createAdminOutsideService(data); break;
        }
      }
      setEditModal(null);
      loadTabData(activeTab);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save');
    }
  };

  const handleDelete = async (type: string, id: number) => {
    try {
      switch (type) {
        case 'material': await api.deleteAdminMaterial(id); break;
        case 'machine': await api.deleteAdminMachine(id); break;
        case 'finish': await api.deleteAdminFinish(id); break;
        case 'labor': await api.deleteAdminLaborRate(id); break;
        case 'service': await api.deleteAdminOutsideService(id); break;
      }
      setDeleteConfirm(null);
      loadTabData(activeTab);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete');
    }
  };

  const handleSeedDefaults = async (type: 'labor' | 'services' | 'all') => {
    try {
      if (type === 'labor' || type === 'all') await api.seedAdminLaborRates();
      if (type === 'services' || type === 'all') await api.seedAdminOutsideServices();
      if (type === 'all') await api.seedQuoteDefaults();
      loadTabData(activeTab);
    } catch (err) {
      console.error('Failed to seed defaults:', err);
    }
  };

  const handleOverheadUpdate = async (key: string, value: string, type: string) => {
    try {
      await api.updateAdminOverhead(key, value, type);
      loadTabData('overhead');
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to update setting');
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
      alert(err.response?.data?.detail || 'Failed to save employee');
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
      alert(err.response?.data?.detail || 'Failed to update employee status');
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
          <p className="page-subtitle">Manage quoting costs and system configuration</p>
        </div>
        <div className="page-actions">
          <button
            onClick={() => handleSeedDefaults('all')}
            className="btn-secondary"
          >
            <ArrowPathIcon className="h-5 w-5 mr-2" />
            Seed Defaults
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-surface-200">
        <nav className="flex gap-1 overflow-x-auto pb-px">
          {tabs.map(tab => (
            <button
              key={tab.key}
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
        {/* Show inactive toggle (not for audit/overhead) */}
        {!['overhead', 'audit'].includes(activeTab) && (
          <div className="flex items-center justify-between mb-4 pb-4 border-b border-surface-200">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={showInactive}
                onChange={(e) => setShowInactive(e.target.checked)}
                className="checkbox"
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
            {activeTab !== 'workcenters' && activeTab !== 'employees' && (
              <button
                onClick={() => setEditModal({ type: activeTab.slice(0, -1), item: null })}
                className="btn-primary btn-sm"
              >
                <PlusIcon className="h-4 w-4 mr-1" />
                Add New
              </button>
            )}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <div className="spinner h-8 w-8"></div>
          </div>
        ) : (
          <>
            {activeTab === 'materials' && <MaterialsTable data={materials} onEdit={(item) => setEditModal({ type: 'material', item })} onDelete={(item) => setDeleteConfirm({ type: 'material', item })} />}
            {activeTab === 'machines' && <MachinesTable data={machines} onEdit={(item) => setEditModal({ type: 'machine', item })} onDelete={(item) => setDeleteConfirm({ type: 'machine', item })} />}
            {activeTab === 'finishes' && <FinishesTable data={finishes} onEdit={(item) => setEditModal({ type: 'finish', item })} onDelete={(item) => setDeleteConfirm({ type: 'finish', item })} />}
            {activeTab === 'labor' && <LaborRatesTable data={laborRates} onEdit={(item) => setEditModal({ type: 'labor', item })} onDelete={(item) => setDeleteConfirm({ type: 'labor', item })} />}
            {activeTab === 'workcenters' && <WorkCenterRatesTable data={workCenterRates} onEdit={(item) => setEditModal({ type: 'workcenter', item })} />}
            {activeTab === 'services' && <OutsideServicesTable data={outsideServices} onEdit={(item) => setEditModal({ type: 'service', item })} onDelete={(item) => setDeleteConfirm({ type: 'service', item })} />}
            {activeTab === 'overhead' && <OverheadSettings data={overhead} onUpdate={handleOverheadUpdate} />}
            {activeTab === 'employees' && (
              <EmployeesTable
                data={filteredEmployees}
                onEdit={(employee) => { setEditingEmployee(employee); setEmployeeModalOpen(true); }}
                onToggleActive={handleEmployeeToggleActive}
              />
            )}
            {activeTab === 'roles' && rolePermissions && <RolePermissionsManager data={rolePermissions} onUpdate={() => loadTabData('roles')} />}
            {activeTab === 'audit' && <AuditLogTable data={auditLog} />}
          </>
        )}
      </div>

      {/* Edit Modal */}
      {editModal && (
        <EditModal
          type={editModal.type}
          item={editModal.item}
          onSave={(data) => handleSave(editModal.type, data)}
          onClose={() => setEditModal(null)}
        />
      )}

      {/* Delete Confirmation */}
      {deleteConfirm && (
        <div className="modal-overlay" onClick={() => setDeleteConfirm(null)}>
          <div className="modal max-w-md" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Confirm Deactivation</h3>
              <button onClick={() => setDeleteConfirm(null)} className="p-2 rounded-lg hover:bg-surface-100">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <div className="modal-body">
              <p className="text-surface-600">
                Are you sure you want to deactivate <strong>{deleteConfirm.item.name}</strong>?
                This can be undone by showing inactive items and editing.
              </p>
            </div>
            <div className="modal-footer">
              <button onClick={() => setDeleteConfirm(null)} className="btn-secondary">Cancel</button>
              <button onClick={() => handleDelete(deleteConfirm.type, deleteConfirm.item.id)} className="btn-danger">Deactivate</button>
            </div>
          </div>
        </div>
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

function MaterialsTable({ data, onEdit, onDelete }: { data: any[]; onEdit: (item: any) => void; onDelete: (item: any) => void }) {
  if (data.length === 0) return <EmptyState message="No materials configured" />;
  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Category</th>
            <th>$/cu.in</th>
            <th>$/lb</th>
            <th>Density</th>
            <th>Machinability</th>
            <th>Markup %</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map(m => (
            <tr key={m.id} className={!m.is_active ? 'opacity-50' : ''}>
              <td className="font-medium">{m.name}</td>
              <td><span className="badge badge-neutral capitalize">{m.category}</span></td>
              <td className="tabular-nums">${m.stock_price_per_cubic_inch?.toFixed(2) || '—'}</td>
              <td className="tabular-nums">${m.stock_price_per_pound?.toFixed(2) || '—'}</td>
              <td className="tabular-nums">{m.density_lb_per_cubic_inch?.toFixed(3) || '—'}</td>
              <td className="tabular-nums">{m.machinability_factor?.toFixed(1)}</td>
              <td className="tabular-nums">{m.material_markup_pct}%</td>
              <td><StatusBadge active={m.is_active} /></td>
              <td><ActionButtons onEdit={() => onEdit(m)} onDelete={() => onDelete(m)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MachinesTable({ data, onEdit, onDelete }: { data: any[]; onEdit: (item: any) => void; onDelete: (item: any) => void }) {
  if (data.length === 0) return <EmptyState message="No machines configured" />;
  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Rate/hr</th>
            <th>Setup Rate</th>
            <th>Setup Hours</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map(m => (
            <tr key={m.id} className={!m.is_active ? 'opacity-50' : ''}>
              <td className="font-medium">{m.name}</td>
              <td><span className="badge badge-neutral">{m.machine_type?.replace(/_/g, ' ')}</span></td>
              <td className="tabular-nums">${m.rate_per_hour?.toFixed(2)}</td>
              <td className="tabular-nums">${m.setup_rate_per_hour?.toFixed(2) || '—'}</td>
              <td className="tabular-nums">{m.typical_setup_hours?.toFixed(1)}h</td>
              <td><StatusBadge active={m.is_active} /></td>
              <td><ActionButtons onEdit={() => onEdit(m)} onDelete={() => onDelete(m)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FinishesTable({ data, onEdit, onDelete }: { data: any[]; onEdit: (item: any) => void; onDelete: (item: any) => void }) {
  if (data.length === 0) return <EmptyState message="No finishes configured" />;
  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Category</th>
            <th>$/part</th>
            <th>$/sqft</th>
            <th>Minimum</th>
            <th>Lead Days</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map(f => (
            <tr key={f.id} className={!f.is_active ? 'opacity-50' : ''}>
              <td className="font-medium">{f.name}</td>
              <td><span className="badge badge-neutral capitalize">{f.category}</span></td>
              <td className="tabular-nums">{f.price_per_part > 0 ? `$${f.price_per_part.toFixed(2)}` : '—'}</td>
              <td className="tabular-nums">{f.price_per_sqft > 0 ? `$${f.price_per_sqft.toFixed(2)}` : '—'}</td>
              <td className="tabular-nums">${f.minimum_charge?.toFixed(2)}</td>
              <td className="tabular-nums">+{f.additional_days}d</td>
              <td><StatusBadge active={f.is_active} /></td>
              <td><ActionButtons onEdit={() => onEdit(f)} onDelete={() => onDelete(f)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LaborRatesTable({ data, onEdit, onDelete }: { data: any[]; onEdit: (item: any) => void; onDelete: (item: any) => void }) {
  if (data.length === 0) return <EmptyState message="No labor rates configured" />;
  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Rate/hr</th>
            <th>Description</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map(l => (
            <tr key={l.id} className={!l.is_active ? 'opacity-50' : ''}>
              <td className="font-medium">{l.name}</td>
              <td className="tabular-nums text-lg font-semibold text-werco-600">${l.rate_per_hour?.toFixed(2)}</td>
              <td className="text-surface-500">{l.description || '—'}</td>
              <td><StatusBadge active={l.is_active} /></td>
              <td><ActionButtons onEdit={() => onEdit(l)} onDelete={() => onDelete(l)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WorkCenterRatesTable({ data, onEdit }: { data: any[]; onEdit: (item: any) => void }) {
  if (data.length === 0) return <EmptyState message="No work centers found" />;
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
                <button onClick={() => onEdit(wc)} className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-50">
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

function OutsideServicesTable({ data, onEdit, onDelete }: { data: any[]; onEdit: (item: any) => void; onDelete: (item: any) => void }) {
  if (data.length === 0) return <EmptyState message="No outside services configured" />;
  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Vendor</th>
            <th>Process</th>
            <th>Cost</th>
            <th>Minimum</th>
            <th>Lead Days</th>
            <th>Status</th>
            <th className="w-24">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.map(s => (
            <tr key={s.id} className={!s.is_active ? 'opacity-50' : ''}>
              <td className="font-medium">{s.name}</td>
              <td className="text-surface-500">{s.vendor_name || '—'}</td>
              <td><span className="badge badge-neutral">{s.process_type?.replace(/_/g, ' ')}</span></td>
              <td className="tabular-nums">${s.default_cost?.toFixed(2)} <span className="text-xs text-surface-400">/{s.cost_unit?.replace('per_', '')}</span></td>
              <td className="tabular-nums">${s.minimum_charge?.toFixed(2)}</td>
              <td className="tabular-nums">{s.typical_lead_days}d</td>
              <td><StatusBadge active={s.is_active} /></td>
              <td><ActionButtons onEdit={() => onEdit(s)} onDelete={() => onDelete(s)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OverheadSettings({ data, onUpdate }: { data: Record<string, any>; onUpdate: (key: string, value: string, type: string) => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');

  const settingDefs = [
    { key: 'default_markup_pct', label: 'Default Markup %', type: 'number', desc: 'Standard markup percentage applied to quotes' },
    { key: 'minimum_order_charge', label: 'Minimum Order Charge', type: 'number', desc: 'Minimum charge for any quote' },
    { key: 'rush_multiplier', label: 'Rush Multiplier', type: 'number', desc: 'Multiplier applied for rush orders (e.g., 1.5 = 50% extra)' },
    { key: 'standard_lead_days', label: 'Standard Lead Days', type: 'number', desc: 'Default lead time in days' },
    { key: 'quantity_breaks', label: 'Quantity Breaks', type: 'json', desc: 'Discount tiers by quantity (JSON format)' },
    { key: 'tolerance_surcharges', label: 'Tolerance Surcharges', type: 'json', desc: 'Surcharge multipliers by tolerance level (JSON format)' },
  ];

  const startEdit = (key: string, currentValue: string) => {
    setEditing(key);
    setEditValue(currentValue);
  };

  const saveEdit = (key: string, type: string) => {
    onUpdate(key, editValue, type);
    setEditing(null);
  };

  return (
    <div className="space-y-4">
      {settingDefs.map(def => {
        const current = data[def.key];
        const displayValue = current?.value ?? '—';
        
        return (
          <div key={def.key} className="flex items-center justify-between p-4 bg-surface-50 rounded-xl">
            <div className="flex-1">
              <p className="font-medium text-surface-900">{def.label}</p>
              <p className="text-sm text-surface-500">{def.desc}</p>
            </div>
            <div className="flex items-center gap-3">
              {editing === def.key ? (
                <>
                  <input
                    type={def.type === 'number' ? 'number' : 'text'}
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    className="input w-48"
                    autoFocus
                  />
                  <button onClick={() => saveEdit(def.key, def.type)} className="p-2 rounded-lg bg-emerald-100 text-emerald-600 hover:bg-emerald-200">
                    <CheckIcon className="h-4 w-4" />
                  </button>
                  <button onClick={() => setEditing(null)} className="p-2 rounded-lg bg-surface-200 text-surface-600 hover:bg-surface-300">
                    <XMarkIcon className="h-4 w-4" />
                  </button>
                </>
              ) : (
                <>
                  <code className="px-3 py-1.5 bg-white border border-surface-200 rounded-lg text-sm font-mono">
                    {def.type === 'json' ? (typeof displayValue === 'string' ? displayValue : JSON.stringify(displayValue)) : displayValue}
                  </code>
                  <button onClick={() => startEdit(def.key, displayValue)} className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-50">
                    <PencilIcon className="h-4 w-4" />
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AuditLogTable({ data }: { data: any[] }) {
  if (data.length === 0) return <EmptyState message="No audit entries" icon={DocumentTextIcon} />;
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
              <td className="text-sm tabular-nums">{format(new Date(entry.changed_at), 'MMM d, yyyy h:mm a')}</td>
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
}: {
  data: EmployeeUser[];
  onEdit: (employee: EmployeeUser) => void;
  onToggleActive: (employee: EmployeeUser) => void;
}) {
  if (data.length === 0) {
    return <EmptyState message="No employees configured yet" icon={UsersIcon} />;
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
                  className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-50"
                  title="Edit"
                >
                  <PencilIcon className="h-4 w-4" />
                </button>
                <button
                  onClick={() => onToggleActive(employee)}
                  className={`p-2 rounded-lg ${employee.is_active ? 'text-surface-500 hover:text-red-600 hover:bg-red-50' : 'text-surface-500 hover:text-werco-600 hover:bg-werco-50'}`}
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
    const normalizedId = normalizeEmployeeId(form.employee_id);
    if (!employee && !EMPLOYEE_ID_PATTERN.test(normalizedId)) {
      setError('Employee ID must be exactly 4 digits.');
      return;
    }
    setError('');
    onSave({ ...form, employee_id: normalizedId });
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal max-w-md" onClick={(e) => e.stopPropagation()}>
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
              <div>
                <label className="label">First Name</label>
                <input
                  className="input"
                  value={form.first_name}
                  onChange={(e) => setForm({ ...form, first_name: e.target.value })}
                  required
                />
              </div>
              <div>
                <label className="label">Last Name</label>
                <input
                  className="input"
                  value={form.last_name}
                  onChange={(e) => setForm({ ...form, last_name: e.target.value })}
                  required
                />
              </div>
            </div>
            <div>
              <label className="label">Employee ID (4 digits)</label>
              <input
                className="input font-mono tracking-widest text-center"
                value={form.employee_id}
                onChange={(e) => setForm({ ...form, employee_id: normalizeEmployeeId(e.target.value) })}
                placeholder="0000"
                maxLength={4}
                inputMode="numeric"
                pattern="\\d{4}"
                disabled={!!employee}
                required
              />
            </div>
            <div>
              <label className="label">Department (optional)</label>
              <input
                className="input"
                value={form.department || ''}
                onChange={(e) => setForm({ ...form, department: e.target.value })}
              />
            </div>
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
  const [selectedRole, setSelectedRole] = useState<string>(data.roles[0]?.value || 'admin');
  const [permissions, setPermissions] = useState<string[]>(data.role_permissions[selectedRole] || []);
  const [saving, setSaving] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);

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
      alert(err.response?.data?.detail || 'Failed to save permissions');
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!window.confirm(`Reset ${selectedRole} permissions to defaults?`)) return;
    setSaving(true);
    try {
      await api.resetRolePermissions(selectedRole);
      onUpdate();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to reset permissions');
    } finally {
      setSaving(false);
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
          <label className="text-sm font-medium text-surface-700">Select Role:</label>
          <select
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
        <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-2 text-sm text-amber-800">
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
                      ? 'bg-amber-100 text-amber-700'
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

function ActionButtons({ onEdit, onDelete }: { onEdit: () => void; onDelete?: () => void }) {
  return (
    <div className="flex items-center gap-1">
      <button onClick={onEdit} className="p-2 rounded-lg text-surface-500 hover:text-werco-600 hover:bg-werco-50">
        <PencilIcon className="h-4 w-4" />
      </button>
      {onDelete && (
        <button onClick={onDelete} className="p-2 rounded-lg text-surface-500 hover:text-red-600 hover:bg-red-50">
          <TrashIcon className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

function EmptyState({ message, icon: Icon = CubeIcon }: { message: string; icon?: React.ComponentType<any> }) {
  return (
    <div className="text-center py-12">
      <Icon className="h-12 w-12 text-surface-300 mx-auto mb-3" />
      <p className="text-surface-500">{message}</p>
    </div>
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
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal max-w-lg" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="text-lg font-semibold">{item ? 'Edit' : 'Add'} {type.charAt(0).toUpperCase() + type.slice(1)}</h3>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-surface-100">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="modal-body space-y-4">
            {type === 'material' && <MaterialForm form={form} update={update} />}
            {type === 'machine' && <MachineForm form={form} update={update} />}
            {type === 'finish' && <FinishForm form={form} update={update} />}
            {type === 'labor' && <LaborForm form={form} update={update} />}
            {type === 'workcenter' && <WorkCenterForm form={form} update={update} />}
            {type === 'service' && <ServiceForm form={form} update={update} />}
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
    case 'material': return { name: '', category: 'steel', stock_price_per_cubic_inch: 0, stock_price_per_pound: 0, density_lb_per_cubic_inch: 0, machinability_factor: 1.0, material_markup_pct: 20 };
    case 'machine': return { name: '', machine_type: 'cnc_mill_3axis', rate_per_hour: 0, setup_rate_per_hour: 0, typical_setup_hours: 1.0 };
    case 'finish': return { name: '', category: 'coating', price_per_part: 0, price_per_sqft: 0, minimum_charge: 0, additional_days: 0 };
    case 'labor': return { name: '', rate_per_hour: 0, description: '' };
    case 'workcenter': return { hourly_rate: 0 };
    case 'service': return { name: '', vendor_name: '', process_type: 'plating', default_cost: 0, cost_unit: 'per_part', minimum_charge: 0, typical_lead_days: 5 };
    default: return {};
  }
}

function MaterialForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <>
      <div>
        <label className="label">Name</label>
        <input className="input" value={form.name} onChange={e => update('name', e.target.value)} required />
      </div>
      <div>
        <label className="label">Category</label>
        <select className="input" value={form.category} onChange={e => update('category', e.target.value)}>
          {MATERIAL_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Price per cu.in ($)</label>
          <input type="number" step="0.01" className="input" value={form.stock_price_per_cubic_inch} onChange={e => update('stock_price_per_cubic_inch', parseFloat(e.target.value) || 0)} />
        </div>
        <div>
          <label className="label">Price per lb ($)</label>
          <input type="number" step="0.01" className="input" value={form.stock_price_per_pound} onChange={e => update('stock_price_per_pound', parseFloat(e.target.value) || 0)} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Density (lb/cu.in)</label>
          <input type="number" step="0.001" className="input" value={form.density_lb_per_cubic_inch} onChange={e => update('density_lb_per_cubic_inch', parseFloat(e.target.value) || 0)} />
        </div>
        <div>
          <label className="label">Machinability Factor</label>
          <input type="number" step="0.1" className="input" value={form.machinability_factor} onChange={e => update('machinability_factor', parseFloat(e.target.value) || 1)} />
        </div>
      </div>
      <div>
        <label className="label">Markup %</label>
        <input type="number" step="1" className="input" value={form.material_markup_pct} onChange={e => update('material_markup_pct', parseFloat(e.target.value) || 0)} />
      </div>
    </>
  );
}

function MachineForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <>
      <div>
        <label className="label">Name</label>
        <input className="input" value={form.name} onChange={e => update('name', e.target.value)} required />
      </div>
      <div>
        <label className="label">Machine Type</label>
        <select className="input" value={form.machine_type} onChange={e => update('machine_type', e.target.value)}>
          {MACHINE_TYPES.map(t => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Rate per Hour ($)</label>
          <input type="number" step="0.01" className="input" value={form.rate_per_hour} onChange={e => update('rate_per_hour', parseFloat(e.target.value) || 0)} required />
        </div>
        <div>
          <label className="label">Setup Rate/hr ($)</label>
          <input type="number" step="0.01" className="input" value={form.setup_rate_per_hour || ''} onChange={e => update('setup_rate_per_hour', parseFloat(e.target.value) || null)} />
        </div>
      </div>
      <div>
        <label className="label">Typical Setup Hours</label>
        <input type="number" step="0.25" className="input" value={form.typical_setup_hours} onChange={e => update('typical_setup_hours', parseFloat(e.target.value) || 0)} />
      </div>
    </>
  );
}

function FinishForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <>
      <div>
        <label className="label">Name</label>
        <input className="input" value={form.name} onChange={e => update('name', e.target.value)} required />
      </div>
      <div>
        <label className="label">Category</label>
        <input className="input" value={form.category} onChange={e => update('category', e.target.value)} placeholder="coating, plating, treatment, etc." />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Price per Part ($)</label>
          <input type="number" step="0.01" className="input" value={form.price_per_part} onChange={e => update('price_per_part', parseFloat(e.target.value) || 0)} />
        </div>
        <div>
          <label className="label">Price per sq.ft ($)</label>
          <input type="number" step="0.01" className="input" value={form.price_per_sqft} onChange={e => update('price_per_sqft', parseFloat(e.target.value) || 0)} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Minimum Charge ($)</label>
          <input type="number" step="0.01" className="input" value={form.minimum_charge} onChange={e => update('minimum_charge', parseFloat(e.target.value) || 0)} />
        </div>
        <div>
          <label className="label">Additional Lead Days</label>
          <input type="number" step="1" className="input" value={form.additional_days} onChange={e => update('additional_days', parseInt(e.target.value) || 0)} />
        </div>
      </div>
    </>
  );
}

function LaborForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <>
      <div>
        <label className="label">Name / Role</label>
        <input className="input" value={form.name} onChange={e => update('name', e.target.value)} required placeholder="e.g., Welder, Machinist, Assembler" />
      </div>
      <div>
        <label className="label">Rate per Hour ($)</label>
        <input type="number" step="0.01" className="input" value={form.rate_per_hour} onChange={e => update('rate_per_hour', parseFloat(e.target.value) || 0)} required />
      </div>
      <div>
        <label className="label">Description</label>
        <textarea className="input" rows={2} value={form.description || ''} onChange={e => update('description', e.target.value)} />
      </div>
    </>
  );
}

function WorkCenterForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <div>
      <label className="label">Hourly Rate ($)</label>
      <input type="number" step="0.01" className="input" value={form.hourly_rate} onChange={e => update('hourly_rate', parseFloat(e.target.value) || 0)} required />
    </div>
  );
}

function ServiceForm({ form, update }: { form: any; update: (f: string, v: any) => void }) {
  return (
    <>
      <div>
        <label className="label">Service Name</label>
        <input className="input" value={form.name} onChange={e => update('name', e.target.value)} required placeholder="e.g., Anodize Type II - ABC Plating" />
      </div>
      <div>
        <label className="label">Vendor Name (optional)</label>
        <input className="input" value={form.vendor_name || ''} onChange={e => update('vendor_name', e.target.value)} placeholder="e.g., ABC Plating Co" />
      </div>
      <div>
        <label className="label">Process Type</label>
        <select className="input" value={form.process_type} onChange={e => update('process_type', e.target.value)}>
          {PROCESS_TYPES.map(t => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Default Cost ($)</label>
          <input type="number" step="0.01" className="input" value={form.default_cost} onChange={e => update('default_cost', parseFloat(e.target.value) || 0)} />
        </div>
        <div>
          <label className="label">Cost Unit</label>
          <select className="input" value={form.cost_unit} onChange={e => update('cost_unit', e.target.value)}>
            {COST_UNITS.map(u => <option key={u} value={u}>{u.replace('per_', 'per ')}</option>)}
          </select>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Minimum Charge ($)</label>
          <input type="number" step="0.01" className="input" value={form.minimum_charge} onChange={e => update('minimum_charge', parseFloat(e.target.value) || 0)} />
        </div>
        <div>
          <label className="label">Typical Lead Days</label>
          <input type="number" step="1" className="input" value={form.typical_lead_days} onChange={e => update('typical_lead_days', parseInt(e.target.value) || 0)} />
        </div>
      </div>
    </>
  );
}
