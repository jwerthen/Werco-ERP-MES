/**
 * `GET /users/departments` (Admin only) — the fixed Department pick list and every user of
 * the company whose stored Department is not exactly on it. Notifications pick recipients by
 * exact department name, so an unlisted value reaches no department-based alert until an
 * Admin picks a listed one (`backend/app/services/user_departments.py`).
 */
export interface UnlistedDepartmentUser {
  id: number;
  name: string;
  employee_id: string;
  is_active: boolean;
  department: string;
}

export interface UserDepartments {
  departments: string[];
  unlisted: UnlistedDepartmentUser[];
}

/**
 * The pick list as the server ships it, used only until `GET /users/departments` answers
 * (or if it fails), so the form never offers a free-text field the server would refuse.
 */
export const FALLBACK_DEPARTMENTS: readonly string[] = [
  'Purchasing',
  'Inventory',
  'Sales',
  'Quality',
  'Maintenance',
  'Shipping',
  'Production',
  'Other',
];
