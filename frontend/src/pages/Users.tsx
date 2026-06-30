import React, { useEffect, useState, useCallback, useMemo } from 'react';
import api from '../services/api';
import { UserRole } from '../types';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { usePermissions } from '../hooks/usePermissions';
import {
  PlusIcon,
  PencilIcon,
  KeyIcon,
  UserMinusIcon,
  UserPlusIcon,
  ArrowUpTrayIcon,
  CheckCircleIcon,
  ClockIcon,
  IdentificationIcon,
  UsersIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../components/ui/Modal';
import {
  useToast,
  DataTable,
  DataTableColumn,
  MobileDataCard,
  FormField,
} from '../components/ui';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import { importTimeoutMessage } from '../utils/apiError';

interface UserData {
  id: number;
  version?: number;
  email: string;
  employee_id: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  department?: string;
  phone?: string;
  is_active: boolean;
  created_at: string;
  last_login?: string;
}

interface UserCsvImportError {
  row: number;
  employee_id?: string;
  email?: string;
  reason: string;
}

interface UserCsvImportResult {
  total_rows: number;
  created_count: number;
  skipped_count: number;
  created_ids: number[];
  errors: UserCsvImportError[];
}

const roleColors: Record<UserRole, string> = {
  platform_admin: 'bg-amber-500/20 text-amber-300',
  admin: 'bg-red-500/20 text-red-300',
  manager: 'bg-purple-500/20 text-purple-800',
  supervisor: 'bg-blue-500/20 text-blue-300',
  operator: 'bg-green-500/20 text-emerald-300',
  quality: 'bg-yellow-500/20 text-yellow-300',
  shipping: 'bg-blue-500/20 text-blue-300',
  viewer: 'bg-slate-800/50 text-slate-100',
};

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

const passwordRequirements = [
  'At least 12 characters',
  'Uppercase and lowercase letters',
  'At least one number',
  'At least one special character',
  'No common words like password, admin, or welcome',
];

const approvableRoles: UserRole[] = ['operator', 'supervisor', 'quality', 'shipping', 'manager', 'admin', 'viewer'];

const EMPTY_FORM = {
  email: '',
  employee_id: '',
  first_name: '',
  last_name: '',
  password: '',
  role: 'operator' as UserRole,
  department: '',
  phone: '',
};

type UserFormData = typeof EMPTY_FORM;

const getApiErrorMessage = (err: any, fallback: string) => {
  const detail = err.response?.data?.detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const path = Array.isArray(item.loc) ? item.loc.filter((part: string) => part !== 'body').join('.') : '';
        return path ? `${path}: ${item.msg}` : item.msg;
      })
      .join('\n');
  }
  return detail || fallback;
};

export default function Users() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { user: currentUser } = useAuth();
  // Badge printing is admin/manager-only: /print/badges loads GET /users, which is
  // server-enforced to ADMIN/MANAGER, so the button mirrors canManageUsers.
  const { canManageUsers } = usePermissions();
  const { showToast } = useToast();
  const approvalMode = searchParams.get('approvals') === 'pending';
  const canApproveUsers =
    currentUser?.role === 'admin' || currentUser?.role === 'platform_admin' || currentUser?.is_superuser === true;
  const [users, setUsers] = useState<UserData[]>([]);
  const [pendingUsers, setPendingUsers] = useState<UserData[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [showInactive, setShowInactive] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [showPasswordModal, setShowPasswordModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [editingUser, setEditingUser] = useState<UserData | null>(null);
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importDefaultPassword, setImportDefaultPassword] = useState('');
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<UserCsvImportResult | null>(null);
  const [approvalRoles, setApprovalRoles] = useState<Record<number, UserRole>>({});
  const [approvingUserIds, setApprovingUserIds] = useState<Record<number, boolean>>({});
  // A0.4: selection for the badge print sheet.
  const [selectedUserIds, setSelectedUserIds] = useState<number[]>([]);

  const [formData, setFormData] = useState<UserFormData>(EMPTY_FORM);
  // Snapshot of the values the Add/Edit form opened with, so an untouched form
  // (create or edit) is never considered dirty. Mirrors formData on open/edit.
  const [initialFormData, setInitialFormData] = useState<UserFormData>(EMPTY_FORM);

  const [newPassword, setNewPassword] = useState('');

  // Unsaved-changes guard for the Add/Edit User modal: prompts on
  // refresh/tab-close while dirty, and gates the Cancel/backdrop close.
  const isFormDirty = useMemo(
    () => showModal && JSON.stringify(formData) !== JSON.stringify(initialFormData),
    [showModal, formData, initialFormData]
  );
  const { confirmDiscard } = useUnsavedChanges(isFormDirty);

  // A0.4: badge-print selection backed by DataTable's selection prop (Set-based).
  const selectedKeySet = React.useMemo(() => new Set<number>(selectedUserIds), [selectedUserIds]);

  const userColumns: Array<DataTableColumn<UserData>> = React.useMemo(
    () => [
      {
        key: 'employee',
        header: 'Employee',
        sortable: true,
        accessor: (u) => `${u.last_name} ${u.first_name}`,
        csv: (u) => `${u.first_name} ${u.last_name}`,
        render: (u) => (
          <div>
            <div className="font-medium">{u.first_name} {u.last_name}</div>
            <div className="text-sm text-slate-400">ID: {u.employee_id}</div>
          </div>
        ),
      },
      {
        key: 'email',
        header: 'Email',
        sortable: true,
        accessor: (u) => u.email,
      },
      {
        key: 'role',
        header: 'Role',
        sortable: true,
        accessor: (u) => roleLabels[u.role],
        render: (u) => (
          <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${roleColors[u.role]}`}>
            {roleLabels[u.role]}
          </span>
        ),
      },
      {
        key: 'department',
        header: 'Department',
        sortable: true,
        accessor: (u) => u.department || '',
        render: (u) => u.department || '-',
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: (u) => (u.is_active ? 'Active' : 'Inactive'),
        render: (u) => (
          <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${
            u.is_active ? 'bg-green-500/20 text-emerald-300' : 'bg-slate-800/50 text-slate-400'
          }`}>
            {u.is_active ? 'Active' : 'Inactive'}
          </span>
        ),
      },
      {
        key: 'actions',
        header: 'Actions',
        align: 'center',
        render: (u) => (
          <div
            className="flex justify-center gap-2"
            role="presentation"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => handleEdit(u)}
              className="text-slate-400 hover:text-slate-400"
              title="Edit"
              aria-label="Edit user"
            >
              <PencilIcon className="h-5 w-5" aria-hidden="true" />
            </button>
            <button
              onClick={() => openPasswordReset(u.id)}
              className="text-slate-400 hover:text-blue-600"
              title="Reset Password"
              aria-label="Reset Password"
            >
              <KeyIcon className="h-5 w-5" aria-hidden="true" />
            </button>
            <button
              onClick={() => handleToggleActive(u)}
              className={u.is_active ? 'text-slate-400 hover:text-red-600' : 'text-slate-400 hover:text-green-600'}
              title={u.is_active ? 'Deactivate' : 'Activate'}
              aria-label={u.is_active ? 'Deactivate user' : 'Activate user'}
            >
              {u.is_active ? (
                <UserMinusIcon className="h-5 w-5" aria-hidden="true" />
              ) : (
                <UserPlusIcon className="h-5 w-5" aria-hidden="true" />
              )}
            </button>
          </div>
        ),
      },
    ],
    []
  );

  const loadUsers = useCallback(async () => {
    setLoadError(false);
    try {
      const [userList, pendingApprovals] = await Promise.all([
        api.getUsers(showInactive || approvalMode),
        canApproveUsers ? api.getPendingUserApprovals() : Promise.resolve([]),
      ]);
      setUsers(userList);
      setPendingUsers(pendingApprovals);
      // Prune badge selections that no longer correspond to a visible user, so a
      // refetch (filter change, approval, deactivation) cannot leave stale ids
      // selected for printing.
      setSelectedUserIds((current) => current.filter((id) => userList.some((u: UserData) => u.id === id)));
    } catch (err) {
      console.error('Failed to load users:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [approvalMode, canApproveUsers, showInactive]);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  useEffect(() => {
    if (approvalMode) {
      setShowInactive(true);
    }
  }, [approvalMode]);

  useEffect(() => {
    setApprovalRoles((current) => {
      const next = { ...current };
      pendingUsers.forEach((user) => {
        if (!next[user.id]) {
          next[user.id] = 'operator';
        }
      });
      return next;
    });
  }, [pendingUsers]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingUser) {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { password: _password, employee_id: _employee_id, ...updateData } = formData;
        await api.updateUser(editingUser.id, {
          ...updateData,
          version: editingUser.version ?? 0,
        });
      } else {
        await api.createUser(formData);
      }
      setShowModal(false);
      resetForm();
      loadUsers();
    } catch (err: any) {
      showToast('error', getApiErrorMessage(err, 'Failed to save user'));
    }
  };

  const handleResetPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedUserId) return;
    try {
      await api.resetUserPassword(selectedUserId, newPassword);
      showToast('success', 'Password reset successfully');
      setShowPasswordModal(false);
      setNewPassword('');
      setSelectedUserId(null);
    } catch (err: any) {
      showToast('error', getApiErrorMessage(err, 'Failed to reset password'));
    }
  };

  const handleToggleActive = async (user: UserData) => {
    try {
      if (user.is_active) {
        await api.deactivateUser(user.id);
      } else {
        await api.activateUser(user.id);
      }
      loadUsers();
    } catch (err: any) {
      showToast('error', getApiErrorMessage(err, 'Failed to update user status'));
    }
  };

  const handleApproveUser = async (user: UserData) => {
    setApprovingUserIds((current) => ({ ...current, [user.id]: true }));
    try {
      await api.approveUser(user.id, {
        role: approvalRoles[user.id] || 'operator',
        department: user.department || undefined,
      });
      await loadUsers();
    } catch (err: any) {
      showToast('error', getApiErrorMessage(err, 'Failed to approve user'));
    } finally {
      setApprovingUserIds((current) => {
        const next = { ...current };
        delete next[user.id];
        return next;
      });
    }
  };

  const handleEdit = (user: UserData) => {
    setEditingUser(user);
    const editValues: UserFormData = {
      email: user.email,
      employee_id: user.employee_id,
      first_name: user.first_name,
      last_name: user.last_name,
      password: '',
      role: user.role,
      department: user.department || '',
      phone: user.phone || '',
    };
    setFormData(editValues);
    setInitialFormData(editValues);
    setShowModal(true);
  };

  useEffect(() => {
    const requestedId = Number(searchParams.get('id') || 0);
    if (!requestedId || users.length === 0 || editingUser?.id === requestedId) return;
    const requestedUser = users.find(user => user.id === requestedId);
    if (requestedUser) handleEdit(requestedUser);
  }, [users, searchParams, editingUser?.id]);

  const openPasswordReset = (userId: number) => {
    setSelectedUserId(userId);
    setNewPassword('');
    setShowPasswordModal(true);
  };

  // A0.4 badge printing
  const toggleBadgeSelection = (userId: number) => {
    setSelectedUserIds((current) =>
      current.includes(userId) ? current.filter((id) => id !== userId) : [...current, userId]
    );
  };

  const handlePrintBadges = () => {
    if (selectedUserIds.length === 0) return;
    window.open(`/print/badges?user_ids=${selectedUserIds.join(',')}`, '_blank');
  };

  const handleImportCsv = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!importFile) {
      showToast('error', 'Please choose a CSV file');
      return;
    }

    setImporting(true);
    try {
      const result = await api.importUsersCsv(importFile, importDefaultPassword);
      setImportResult(result);
      setShowImportModal(false);
      setImportFile(null);
      setImportDefaultPassword('');
      if (result.created_count > 0) {
        await loadUsers();
      }
    } catch (err: any) {
      // This modal commits directly (no dry run), so an Axios timeout means the server
      // may still be importing — translate it before falling back to the generic handler.
      showToast('error', importTimeoutMessage(err, 'commit') ?? getApiErrorMessage(err, 'Failed to import CSV'));
    } finally {
      setImporting(false);
    }
  };

  const resetForm = () => {
    setEditingUser(null);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete('id');
    setSearchParams(nextParams, { replace: true });
    setFormData(EMPTY_FORM);
    setInitialFormData(EMPTY_FORM);
  };

  // Gate the Add/Edit modal's Cancel/backdrop close behind the dirty-confirm.
  // The successful-submit path calls setShowModal(false)/resetForm() directly,
  // so saving never triggers the discard prompt.
  const requestCloseModal = () => {
    if (!confirmDiscard()) return;
    setShowModal(false);
    resetForm();
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-white">User Management</h1>
          {pendingUsers.length > 0 && (
            <p className="mt-1 text-sm text-amber-300">
              {pendingUsers.length} account{pendingUsers.length === 1 ? '' : 's'} awaiting approval
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {pendingUsers.length > 0 && (
            <button
              onClick={() => {
                const nextParams = new URLSearchParams(searchParams);
                nextParams.set('approvals', 'pending');
                setSearchParams(nextParams, { replace: true });
                setShowInactive(true);
              }}
              className="btn-secondary flex items-center"
            >
              <ClockIcon className="h-5 w-5 mr-2 text-amber-300" />
              Pending ({pendingUsers.length})
            </button>
          )}
          {canManageUsers && (
            <button
              onClick={handlePrintBadges}
              className="btn-secondary flex items-center"
              disabled={selectedUserIds.length === 0}
              title={selectedUserIds.length === 0 ? 'Select users below to print badges' : 'Print badges for selected users'}
            >
              <IdentificationIcon className="h-5 w-5 mr-2" />
              Print Badges{selectedUserIds.length > 0 ? ` (${selectedUserIds.length})` : ''}
            </button>
          )}
          <button
            onClick={() => setShowImportModal(true)}
            className="btn-secondary flex items-center"
          >
            <ArrowUpTrayIcon className="h-5 w-5 mr-2" />
            Import CSV
          </button>
          <button
            onClick={() => { resetForm(); setShowModal(true); }}
            className="btn-primary flex items-center"
          >
            <PlusIcon className="h-5 w-5 mr-2" />
            Add User
          </button>
        </div>
      </div>

      <div className="flex items-center">
        <label className="flex items-center cursor-pointer">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={(e) => setShowInactive(e.target.checked)}
            className="mr-2 rounded border-slate-600"
            aria-label="Show inactive users"
          />
          <span className="text-sm text-slate-300">Show inactive users</span>
        </label>
      </div>

      {importResult && (
        <div className="card">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <div className="text-sm text-slate-300">
              Imported rows: <span className="font-semibold">{importResult.total_rows}</span>
            </div>
            <div className="text-sm text-emerald-400">
              Created: <span className="font-semibold">{importResult.created_count}</span>
            </div>
            <div className="text-sm text-amber-400">
              Skipped: <span className="font-semibold">{importResult.skipped_count}</span>
            </div>
          </div>
          {importResult.errors.length > 0 && (
            <div className="mt-3 border-t border-slate-700 pt-3">
              <p className="text-sm font-medium text-white mb-2">Import issues</p>
              <ul className="space-y-1 text-sm text-slate-300 max-h-40 overflow-auto">
                {importResult.errors.map((error, idx) => (
                  <li key={`${error.row}-${idx}`}>
                    Row {error.row}: {error.reason}
                    {error.employee_id ? ` (employee_id: ${error.employee_id})` : ''}
                    {error.email ? ` (email: ${error.email})` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {pendingUsers.length > 0 && (
        <div className="card border-amber-500/30 bg-amber-500/5">
          <div className="card-header">
            <div>
              <h2 className="card-title flex items-center gap-2">
                <ClockIcon className="h-5 w-5 text-amber-300" />
                Pending Account Approvals
              </h2>
              <p className="card-subtitle">New self-registered accounts are inactive until an admin approves them.</p>
            </div>
          </div>
          <div className="mt-4 grid gap-3">
            {pendingUsers.map((user) => (
              <div
                key={user.id}
                className="grid gap-3 rounded-[4px] border border-slate-700 bg-[#101722] p-4 lg:grid-cols-[minmax(0,1fr)_180px_120px] lg:items-center"
              >
                <div className="min-w-0">
                  <div className="font-medium text-white">{user.first_name} {user.last_name}</div>
                  <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-400">
                    <span>{user.email}</span>
                    <span>ID: {user.employee_id}</span>
                    <span>Requested {new Date(user.created_at).toLocaleDateString()}</span>
                  </div>
                </div>
                <select
                  value={approvalRoles[user.id] || 'operator'}
                  onChange={(e) =>
                    setApprovalRoles((current) => ({
                      ...current,
                      [user.id]: e.target.value as UserRole,
                    }))
                  }
                  className="input h-10"
                  aria-label={`Approval role for ${user.first_name} ${user.last_name}`}
                >
                  {approvableRoles.map((role) => (
                    <option key={role} value={role}>{roleLabels[role]}</option>
                  ))}
                </select>
                <button
                  onClick={() => handleApproveUser(user)}
                  className="btn-primary h-10 justify-center"
                  disabled={!!approvingUserIds[user.id]}
                >
                  <CheckCircleIcon className="h-5 w-5 mr-2" />
                  {approvingUserIds[user.id] ? 'Approving' : 'Approve'}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <DataTable<UserData>
        columns={userColumns}
        data={users}
        rowKey={(u) => u.id}
        loading={loading}
        error={loadError}
        onRetry={loadUsers}
        defaultSort={{ key: 'employee', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: 'users' }}
        selection={{
          selectedKeys: selectedKeySet as Set<string | number>,
          onChange: (keys) => setSelectedUserIds(Array.from(keys).map((k) => Number(k))),
        }}
        empty={{
          icon: UsersIcon,
          title: 'No users found',
          description: showInactive
            ? 'No users match the current filter.'
            : 'Add a user or import a CSV to get started.',
          action: canManageUsers
            ? { label: 'Add User', onClick: () => { resetForm(); setShowModal(true); } }
            : undefined,
        }}
        mobileCards={(user) => (
          <MobileDataCard
            key={user.id}
            title={`${user.first_name} ${user.last_name}`}
            subtitle={`ID: ${user.employee_id}`}
            className={!user.is_active ? 'opacity-60' : ''}
            badge={
              <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${roleColors[user.role]}`}>
                {roleLabels[user.role]}
              </span>
            }
            fields={[
              { label: 'Email', value: user.email, fullWidth: true },
              { label: 'Department', value: user.department || '-' },
              {
                label: 'Status',
                value: (
                  <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                    user.is_active ? 'bg-green-500/20 text-emerald-300' : 'bg-slate-800/50 text-slate-400'
                  }`}>
                    {user.is_active ? 'Active' : 'Inactive'}
                  </span>
                ),
              },
            ]}
            actions={
              <>
                <label className="flex items-center gap-1.5 text-xs text-slate-300 mr-auto cursor-pointer">
                  <input
                    type="checkbox"
                    checked={selectedUserIds.includes(user.id)}
                    onChange={() => toggleBadgeSelection(user.id)}
                    className="rounded border-slate-600"
                    aria-label={`Select ${user.first_name} ${user.last_name} for badge printing`}
                  />
                  Badge
                </label>
                <button
                  onClick={() => handleEdit(user)}
                  className="text-slate-400 hover:text-slate-200"
                  title="Edit"
                  aria-label="Edit user"
                >
                  <PencilIcon className="h-5 w-5" aria-hidden="true" />
                </button>
                <button
                  onClick={() => openPasswordReset(user.id)}
                  className="text-slate-400 hover:text-blue-600"
                  title="Reset Password"
                  aria-label="Reset Password"
                >
                  <KeyIcon className="h-5 w-5" aria-hidden="true" />
                </button>
                <button
                  onClick={() => handleToggleActive(user)}
                  className={user.is_active ? 'text-slate-400 hover:text-red-600' : 'text-slate-400 hover:text-green-600'}
                  title={user.is_active ? 'Deactivate' : 'Activate'}
                  aria-label={user.is_active ? 'Deactivate user' : 'Activate user'}
                >
                  {user.is_active ? (
                    <UserMinusIcon className="h-5 w-5" aria-hidden="true" />
                  ) : (
                    <UserPlusIcon className="h-5 w-5" aria-hidden="true" />
                  )}
                </button>
              </>
            }
          />
        )}
      />

      {/* Add/Edit User Modal */}
      <Modal
        open={showModal}
        onClose={requestCloseModal}
        size="lg"
        closeOnBackdrop={false}
      >
            <h3 className="text-lg font-semibold mb-4">
              {editingUser ? 'Edit User' : 'Add User'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="First Name" required>
                  {(field) => (
                    <input
                      {...field}
                      autoFocus
                      type="text"
                      value={formData.first_name}
                      onChange={(e) => setFormData({ ...formData, first_name: e.target.value })}
                      className="input"
                      required
                    />
                  )}
                </FormField>
                <FormField label="Last Name" required>
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.last_name}
                      onChange={(e) => setFormData({ ...formData, last_name: e.target.value })}
                      className="input"
                      required
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Employee ID" required>
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.employee_id}
                      onChange={(e) => setFormData({ ...formData, employee_id: e.target.value })}
                      className="input"
                      required
                      disabled={!!editingUser}
                    />
                  )}
                </FormField>
                <FormField label="Email" required>
                  {(field) => (
                    <input
                      {...field}
                      type="email"
                      value={formData.email}
                      onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                      className="input"
                      required
                    />
                  )}
                </FormField>
              </div>

              {!editingUser && (
                <FormField label="Password" required help={
                  <ul className="space-y-1">
                    {passwordRequirements.map((requirement) => (
                      <li key={requirement}>{requirement}</li>
                    ))}
                  </ul>
                }>
                  {(field) => (
                    <input
                      {...field}
                      type="password"
                      value={formData.password}
                      onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                      className="input"
                      required
                      minLength={12}
                      autoComplete="new-password"
                    />
                  )}
                </FormField>
              )}

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Role" required>
                  {(field) => (
                    <select
                      {...field}
                      value={formData.role}
                      onChange={(e) => setFormData({ ...formData, role: e.target.value as UserRole })}
                      className="input"
                      required
                    >
                      <option value="operator">Operator</option>
                      <option value="supervisor">Supervisor</option>
                      <option value="quality">Quality</option>
                      <option value="shipping">Shipping</option>
                      <option value="manager">Manager</option>
                      <option value="admin">Administrator</option>
                      <option value="viewer">View Only</option>
                    </select>
                  )}
                </FormField>
                <FormField label="Department">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.department}
                      onChange={(e) => setFormData({ ...formData, department: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
              </div>

              <div className="flex justify-end gap-3 mt-6">
                <button type="button" onClick={requestCloseModal} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  {editingUser ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
      </Modal>

      {/* Reset Password Modal */}
      <Modal
        open={showPasswordModal}
        onClose={() => setShowPasswordModal(false)}
        size="sm"
        closeOnBackdrop={false}
      >
            <h3 className="text-lg font-semibold mb-4">Reset Password</h3>
            <form onSubmit={handleResetPassword} className="space-y-4">
              <FormField label="New Password" required help={
                <ul className="space-y-1">
                  {passwordRequirements.map((requirement) => (
                    <li key={requirement}>{requirement}</li>
                  ))}
                </ul>
              }>
                {(field) => (
                  <input
                    {...field}
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    className="input"
                    required
                    minLength={12}
                    autoComplete="new-password"
                  />
                )}
              </FormField>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowPasswordModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Reset Password</button>
              </div>
            </form>
      </Modal>

      {/* CSV Import Modal */}
      <Modal
        open={showImportModal}
        onClose={() => {
          setShowImportModal(false);
          setImportFile(null);
          setImportDefaultPassword('');
        }}
        size="lg"
        closeOnBackdrop={false}
        closeOnEscape={!importing}
      >
            <h3 className="text-lg font-semibold mb-4">Import Users From CSV</h3>
            <form onSubmit={handleImportCsv} className="space-y-4">
              <FormField label="CSV File" required>
                {(field) => (
                  <input
                    {...field}
                    type="file"
                    accept=".csv,text/csv"
                    onChange={(e) => setImportFile(e.target.files?.[0] || null)}
                    className="input"
                    required
                  />
                )}
              </FormField>
              <FormField label="Default Password (optional)">
                {(field) => (
                  <input
                    {...field}
                    type="password"
                    value={importDefaultPassword}
                    onChange={(e) => setImportDefaultPassword(e.target.value)}
                    className="input"
                    placeholder="Used when a CSV row does not include password"
                  />
                )}
              </FormField>
              <div className="text-xs text-slate-400 bg-slate-800 border border-slate-700 rounded p-3">
                <p className="font-medium text-slate-100 mb-1">CSV columns</p>
                <p>Required: employee_id, first_name, last_name</p>
                <p>Optional: email, password, role, department</p>
                <p className="mt-1">
                  Operators can omit passwords and use employee-ID login only. For non-operators, provide a password
                  in CSV or set a default password.
                </p>
              </div>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => {
                    setShowImportModal(false);
                    setImportFile(null);
                    setImportDefaultPassword('');
                  }}
                  className="btn-secondary"
                  disabled={importing}
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={importing}>
                  {importing ? 'Importing...' : 'Import'}
                </button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
