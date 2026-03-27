"""Create tables for tools, OEE, downtime, job costing, maintenance,
operator certifications, engineering changes, SPC, customer complaints,
supplier scorecards, and related child tables.

Revision ID: 024_add_missing_module_tables
Revises: 023_add_qms_standards
Create Date: 2026-03-27

These models existed in code but were previously only created via
Base.metadata.create_all() in non-production environments.
This migration ensures they exist in production (Alembic-managed) databases.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision = '024_add_missing_module_tables'
down_revision = '023_add_qms_standards'
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    return name in inspector.get_table_names()


def _create_table_if_not_exists(name: str, *columns, **kwargs):
    if not _table_exists(name):
        _create_table_if_not_exists(name, *columns, **kwargs)


def upgrade() -> None:

    # ===== OEE =====
    _create_table_if_not_exists(
        'oee_records',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=False, index=True),
        sa.Column('record_date', sa.Date(), nullable=True, index=True),
        sa.Column('shift', sa.String(50), nullable=True),
        sa.Column('planned_production_time_minutes', sa.Float(), server_default='0'),
        sa.Column('actual_run_time_minutes', sa.Float(), server_default='0'),
        sa.Column('downtime_minutes', sa.Float(), server_default='0'),
        sa.Column('total_parts_produced', sa.Integer(), server_default='0'),
        sa.Column('ideal_cycle_time_seconds', sa.Float(), server_default='0'),
        sa.Column('actual_operating_time_minutes', sa.Float(), server_default='0'),
        sa.Column('good_parts', sa.Integer(), server_default='0'),
        sa.Column('total_parts', sa.Integer(), server_default='0'),
        sa.Column('defect_parts', sa.Integer(), server_default='0'),
        sa.Column('rework_parts', sa.Integer(), server_default='0'),
        sa.Column('availability_pct', sa.Float(), server_default='0'),
        sa.Column('performance_pct', sa.Float(), server_default='0'),
        sa.Column('quality_pct', sa.Float(), server_default='0'),
        sa.Column('oee_pct', sa.Float(), server_default='0'),
        sa.Column('unplanned_stop_minutes', sa.Float(), server_default='0'),
        sa.Column('planned_stop_minutes', sa.Float(), server_default='0'),
        sa.Column('small_stop_minutes', sa.Float(), server_default='0'),
        sa.Column('slow_cycle_minutes', sa.Float(), server_default='0'),
        sa.Column('production_reject_count', sa.Integer(), server_default='0'),
        sa.Column('startup_reject_count', sa.Integer(), server_default='0'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'oee_targets',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), unique=True),
        sa.Column('target_oee_pct', sa.Float(), server_default='85.0'),
        sa.Column('target_availability_pct', sa.Float(), server_default='90.0'),
        sa.Column('target_performance_pct', sa.Float(), server_default='95.0'),
        sa.Column('target_quality_pct', sa.Float(), server_default='99.0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Downtime =====
    _create_table_if_not_exists(
        'downtime_reason_codes',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('code', sa.String(50), unique=True, index=True, nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('category', sa.String(50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('display_order', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'downtime_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=False, index=True),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=True),
        sa.Column('start_time', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('end_time', sa.DateTime(), nullable=True),
        sa.Column('duration_minutes', sa.Float(), nullable=True),
        sa.Column('category', sa.String(50), server_default='other'),
        sa.Column('planned_type', sa.String(50), server_default='unplanned'),
        sa.Column('reason_code', sa.String(50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('resolution', sa.Text(), nullable=True),
        sa.Column('reported_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('resolved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Job Costing =====
    _create_table_if_not_exists(
        'job_costs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), unique=True, nullable=False),
        sa.Column('estimated_material_cost', sa.Float(), server_default='0'),
        sa.Column('estimated_labor_cost', sa.Float(), server_default='0'),
        sa.Column('estimated_overhead_cost', sa.Float(), server_default='0'),
        sa.Column('estimated_total_cost', sa.Float(), server_default='0'),
        sa.Column('actual_material_cost', sa.Float(), server_default='0'),
        sa.Column('actual_labor_cost', sa.Float(), server_default='0'),
        sa.Column('actual_overhead_cost', sa.Float(), server_default='0'),
        sa.Column('actual_total_cost', sa.Float(), server_default='0'),
        sa.Column('material_variance', sa.Float(), server_default='0'),
        sa.Column('labor_variance', sa.Float(), server_default='0'),
        sa.Column('overhead_variance', sa.Float(), server_default='0'),
        sa.Column('total_variance', sa.Float(), server_default='0'),
        sa.Column('margin_amount', sa.Float(), server_default='0'),
        sa.Column('margin_percent', sa.Float(), server_default='0'),
        sa.Column('revenue', sa.Float(), server_default='0'),
        sa.Column('status', sa.String(50), server_default='in_progress', index=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'cost_entries',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_cost_id', sa.Integer(), sa.ForeignKey('job_costs.id'), nullable=False),
        sa.Column('entry_type', sa.String(50), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('quantity', sa.Float(), server_default='1'),
        sa.Column('unit_cost', sa.Float(), server_default='0'),
        sa.Column('total_cost', sa.Float(), server_default='0'),
        sa.Column('work_order_operation_id', sa.Integer(), sa.ForeignKey('work_order_operations.id'), nullable=True),
        sa.Column('source', sa.String(50), server_default='manual'),
        sa.Column('reference', sa.String(255), nullable=True),
        sa.Column('entry_date', sa.Date(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Tools =====
    _create_table_if_not_exists(
        'tools',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('tool_id', sa.String(50), unique=True, index=True, nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('tool_type', sa.String(50), server_default='other'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('manufacturer', sa.String(255), nullable=True),
        sa.Column('model_number', sa.String(100), nullable=True),
        sa.Column('serial_number', sa.String(100), nullable=True),
        sa.Column('status', sa.String(50), server_default='available'),
        sa.Column('location', sa.String(255), nullable=True),
        sa.Column('current_work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=True),
        sa.Column('current_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('max_life_hours', sa.Float(), nullable=True),
        sa.Column('current_life_hours', sa.Float(), server_default='0'),
        sa.Column('max_life_cycles', sa.Integer(), nullable=True),
        sa.Column('current_life_cycles', sa.Integer(), server_default='0'),
        sa.Column('life_remaining_pct', sa.Float(), nullable=True),
        sa.Column('purchase_date', sa.Date(), nullable=True),
        sa.Column('purchase_cost', sa.Float(), server_default='0'),
        sa.Column('last_inspection_date', sa.Date(), nullable=True),
        sa.Column('next_inspection_date', sa.Date(), nullable=True),
        sa.Column('inspection_interval_days', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'tool_checkouts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('tool_id', sa.Integer(), sa.ForeignKey('tools.id'), nullable=False),
        sa.Column('checked_out_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('checked_out_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('checked_in_at', sa.DateTime(), nullable=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=True),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=True),
        sa.Column('condition_out', sa.String(50), server_default='good'),
        sa.Column('condition_in', sa.String(50), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'tool_usage_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('tool_id', sa.Integer(), sa.ForeignKey('tools.id'), nullable=False),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=True),
        sa.Column('usage_hours', sa.Float(), server_default='0'),
        sa.Column('usage_cycles', sa.Integer(), server_default='0'),
        sa.Column('usage_date', sa.Date(), nullable=True),
        sa.Column('recorded_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Maintenance =====
    _create_table_if_not_exists(
        'maintenance_schedules',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('maintenance_type', sa.String(50), server_default='preventive'),
        sa.Column('frequency', sa.String(50), nullable=True),
        sa.Column('frequency_days', sa.Integer(), nullable=True),
        sa.Column('estimated_duration_hours', sa.Float(), server_default='1.0'),
        sa.Column('priority', sa.String(50), server_default='medium'),
        sa.Column('checklist', sa.Text(), nullable=True),
        sa.Column('requires_shutdown', sa.Boolean(), server_default='false'),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('last_completed_date', sa.Date(), nullable=True),
        sa.Column('next_due_date', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'maintenance_work_orders',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('schedule_id', sa.Integer(), sa.ForeignKey('maintenance_schedules.id'), nullable=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=False),
        sa.Column('wo_number', sa.String(50), unique=True, index=True, nullable=False),
        sa.Column('maintenance_type', sa.String(50), server_default='preventive'),
        sa.Column('priority', sa.String(50), server_default='medium'),
        sa.Column('status', sa.String(50), server_default='scheduled'),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('checklist_results', sa.Text(), nullable=True),
        sa.Column('scheduled_date', sa.Date(), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('actual_duration_hours', sa.Float(), nullable=True),
        sa.Column('requires_shutdown', sa.Boolean(), server_default='false'),
        sa.Column('downtime_minutes', sa.Float(), server_default='0'),
        sa.Column('parts_used', sa.Text(), nullable=True),
        sa.Column('labor_cost', sa.Float(), server_default='0'),
        sa.Column('parts_cost', sa.Float(), server_default='0'),
        sa.Column('total_cost', sa.Float(), server_default='0'),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('completed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('findings', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'maintenance_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=False),
        sa.Column('maintenance_wo_id', sa.Integer(), sa.ForeignKey('maintenance_work_orders.id'), nullable=True),
        sa.Column('event_type', sa.String(50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('performed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('event_date', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('cost', sa.Float(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Operator Certifications =====
    _create_table_if_not_exists(
        'operator_certifications',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('certification_type', sa.String(50), nullable=False),
        sa.Column('certification_name', sa.String(255), nullable=False),
        sa.Column('issuing_authority', sa.String(255), nullable=True),
        sa.Column('certificate_number', sa.String(100), nullable=True),
        sa.Column('issue_date', sa.Date(), nullable=True),
        sa.Column('expiration_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(50), server_default='active'),
        sa.Column('level', sa.String(50), nullable=True),
        sa.Column('scope', sa.Text(), nullable=True),
        sa.Column('document_reference', sa.String(255), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('verified_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('verified_date', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'training_records',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('training_name', sa.String(255), nullable=False),
        sa.Column('training_type', sa.String(100), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('trainer', sa.String(255), nullable=True),
        sa.Column('training_date', sa.Date(), nullable=True),
        sa.Column('completion_date', sa.Date(), nullable=True),
        sa.Column('hours', sa.Float(), nullable=True),
        sa.Column('passed', sa.Boolean(), server_default='true'),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('certificate_number', sa.String(100), nullable=True),
        sa.Column('expiration_date', sa.Date(), nullable=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('recorded_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'skill_matrix',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=False, index=True),
        sa.Column('skill_level', sa.Integer(), server_default='1'),
        sa.Column('qualified_date', sa.Date(), nullable=True),
        sa.Column('last_assessment_date', sa.Date(), nullable=True),
        sa.Column('next_assessment_date', sa.Date(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'work_center_id', name='uq_user_work_center'),
    )

    # ===== Engineering Changes =====
    _create_table_if_not_exists(
        'engineering_change_orders',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('eco_number', sa.String(50), unique=True, index=True, nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('eco_type', sa.String(50), nullable=True),
        sa.Column('priority', sa.String(50), server_default='medium'),
        sa.Column('status', sa.String(50), server_default='draft'),
        sa.Column('reason_for_change', sa.Text(), nullable=True),
        sa.Column('proposed_solution', sa.Text(), nullable=True),
        sa.Column('impact_analysis', sa.Text(), nullable=True),
        sa.Column('risk_assessment', sa.Text(), nullable=True),
        sa.Column('affected_parts', sa.Text(), nullable=True),
        sa.Column('affected_work_orders', sa.Text(), nullable=True),
        sa.Column('affected_documents', sa.Text(), nullable=True),
        sa.Column('estimated_cost', sa.Float(), server_default='0'),
        sa.Column('actual_cost', sa.Float(), server_default='0'),
        sa.Column('effectivity_type', sa.String(50), nullable=True),
        sa.Column('effectivity_date', sa.Date(), nullable=True),
        sa.Column('effectivity_serial', sa.String(100), nullable=True),
        sa.Column('requested_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_date', sa.DateTime(), nullable=True),
        sa.Column('target_date', sa.Date(), nullable=True),
        sa.Column('completed_date', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'eco_approvals',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('eco_id', sa.Integer(), sa.ForeignKey('engineering_change_orders.id'), nullable=False),
        sa.Column('approver_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('role', sa.String(100), nullable=True),
        sa.Column('status', sa.String(50), server_default='pending'),
        sa.Column('comments', sa.Text(), nullable=True),
        sa.Column('decision_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'eco_implementation_tasks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('eco_id', sa.Integer(), sa.ForeignKey('engineering_change_orders.id'), nullable=False),
        sa.Column('task_number', sa.Integer(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('department', sa.String(100), nullable=True),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('status', sa.String(50), server_default='pending'),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('completed_date', sa.Date(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== SPC =====
    _create_table_if_not_exists(
        'spc_characteristics',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=True),
        sa.Column('characteristic_type', sa.String(50), nullable=True),
        sa.Column('unit_of_measure', sa.String(50), nullable=True),
        sa.Column('specification_nominal', sa.Float(), nullable=True),
        sa.Column('specification_usl', sa.Float(), nullable=True),
        sa.Column('specification_lsl', sa.Float(), nullable=True),
        sa.Column('chart_type', sa.String(50), server_default='xbar_r'),
        sa.Column('subgroup_size', sa.Integer(), server_default='5'),
        sa.Column('work_center_id', sa.Integer(), sa.ForeignKey('work_centers.id'), nullable=True),
        sa.Column('operation_number', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('is_critical', sa.Boolean(), server_default='false'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'spc_control_limits',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('characteristic_id', sa.Integer(), sa.ForeignKey('spc_characteristics.id'), nullable=False),
        sa.Column('calculation_date', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('ucl', sa.Float(), nullable=True),
        sa.Column('lcl', sa.Float(), nullable=True),
        sa.Column('center_line', sa.Float(), nullable=True),
        sa.Column('ucl_range', sa.Float(), nullable=True),
        sa.Column('lcl_range', sa.Float(), nullable=True),
        sa.Column('center_line_range', sa.Float(), nullable=True),
        sa.Column('sample_count', sa.Integer(), nullable=True),
        sa.Column('is_current', sa.Boolean(), server_default='true'),
        sa.Column('calculated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'spc_measurements',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('characteristic_id', sa.Integer(), sa.ForeignKey('spc_characteristics.id'), nullable=False),
        sa.Column('subgroup_number', sa.Integer(), nullable=True),
        sa.Column('measurement_value', sa.Float(), nullable=True),
        sa.Column('sample_number', sa.Integer(), nullable=True),
        sa.Column('measured_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('measured_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=True),
        sa.Column('lot_number', sa.String(100), nullable=True),
        sa.Column('serial_number', sa.String(100), nullable=True),
        sa.Column('is_out_of_control', sa.Boolean(), server_default='false'),
        sa.Column('violation_rules', sa.String(255), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'spc_process_capabilities',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('characteristic_id', sa.Integer(), sa.ForeignKey('spc_characteristics.id'), nullable=False),
        sa.Column('study_date', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('sample_count', sa.Integer(), nullable=True),
        sa.Column('mean', sa.Float(), nullable=True),
        sa.Column('std_dev', sa.Float(), nullable=True),
        sa.Column('cp', sa.Float(), nullable=True),
        sa.Column('cpk', sa.Float(), nullable=True),
        sa.Column('pp', sa.Float(), nullable=True),
        sa.Column('ppk', sa.Float(), nullable=True),
        sa.Column('within_spec_pct', sa.Float(), nullable=True),
        sa.Column('is_capable', sa.Boolean(), server_default='false'),
        sa.Column('performed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Customer Complaints & RMA =====
    _create_table_if_not_exists(
        'customer_complaints',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('complaint_number', sa.String(50), unique=True, index=True, nullable=False),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=True),
        sa.Column('customer_name', sa.String(255), nullable=True),
        sa.Column('customer_po_number', sa.String(100), nullable=True),
        sa.Column('customer_contact', sa.String(255), nullable=True),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=True),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=True),
        sa.Column('lot_number', sa.String(100), nullable=True),
        sa.Column('serial_number', sa.String(100), nullable=True),
        sa.Column('quantity_affected', sa.Float(), server_default='1'),
        sa.Column('severity', sa.String(50), server_default='minor'),
        sa.Column('status', sa.String(50), server_default='received'),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('date_received', sa.Date(), nullable=True),
        sa.Column('date_of_occurrence', sa.Date(), nullable=True),
        sa.Column('investigation_findings', sa.Text(), nullable=True),
        sa.Column('root_cause', sa.Text(), nullable=True),
        sa.Column('containment_action', sa.Text(), nullable=True),
        sa.Column('corrective_action', sa.Text(), nullable=True),
        sa.Column('preventive_action', sa.Text(), nullable=True),
        sa.Column('resolution_description', sa.Text(), nullable=True),
        sa.Column('ncr_id', sa.Integer(), sa.ForeignKey('ncrs.id'), nullable=True),
        sa.Column('car_id', sa.Integer(), sa.ForeignKey('cars.id'), nullable=True),
        sa.Column('estimated_cost', sa.Float(), server_default='0'),
        sa.Column('actual_cost', sa.Float(), server_default='0'),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('received_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('resolved_date', sa.Date(), nullable=True),
        sa.Column('closed_date', sa.Date(), nullable=True),
        sa.Column('customer_satisfied', sa.Boolean(), nullable=True),
        sa.Column('satisfaction_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'return_material_authorizations',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('rma_number', sa.String(50), unique=True, index=True, nullable=False),
        sa.Column('complaint_id', sa.Integer(), sa.ForeignKey('customer_complaints.id'), nullable=True),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=True),
        sa.Column('customer_name', sa.String(255), nullable=True),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=True),
        sa.Column('status', sa.String(50), server_default='requested'),
        sa.Column('quantity', sa.Float(), nullable=True),
        sa.Column('lot_number', sa.String(100), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('disposition', sa.String(100), nullable=True),
        sa.Column('shipping_tracking', sa.String(255), nullable=True),
        sa.Column('received_date', sa.Date(), nullable=True),
        sa.Column('inspection_date', sa.Date(), nullable=True),
        sa.Column('inspection_findings', sa.Text(), nullable=True),
        sa.Column('replacement_wo_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=True),
        sa.Column('credit_amount', sa.Float(), server_default='0'),
        sa.Column('authorized_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('authorized_date', sa.Date(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ===== Supplier Scorecards =====
    _create_table_if_not_exists(
        'supplier_scorecards',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('vendors.id'), nullable=False, index=True),
        sa.Column('period_type', sa.String(50), server_default='quarterly'),
        sa.Column('period_start', sa.Date(), nullable=True),
        sa.Column('period_end', sa.Date(), nullable=True),
        sa.Column('quality_score', sa.Float(), server_default='0'),
        sa.Column('quality_weight', sa.Float(), server_default='0.40'),
        sa.Column('delivery_score', sa.Float(), server_default='0'),
        sa.Column('delivery_weight', sa.Float(), server_default='0.30'),
        sa.Column('responsiveness_score', sa.Float(), server_default='0'),
        sa.Column('responsiveness_weight', sa.Float(), server_default='0.15'),
        sa.Column('price_score', sa.Float(), server_default='0'),
        sa.Column('price_weight', sa.Float(), server_default='0.15'),
        sa.Column('overall_score', sa.Float(), server_default='0'),
        sa.Column('rating', sa.String(20), nullable=True),
        sa.Column('total_pos', sa.Integer(), server_default='0'),
        sa.Column('total_lines', sa.Integer(), server_default='0'),
        sa.Column('on_time_deliveries', sa.Integer(), server_default='0'),
        sa.Column('late_deliveries', sa.Integer(), server_default='0'),
        sa.Column('total_received_qty', sa.Float(), server_default='0'),
        sa.Column('rejected_qty', sa.Float(), server_default='0'),
        sa.Column('ncr_count', sa.Integer(), server_default='0'),
        sa.Column('car_count', sa.Integer(), server_default='0'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('action_items', sa.Text(), nullable=True),
        sa.Column('evaluated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'supplier_audits',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('vendors.id'), nullable=False, index=True),
        sa.Column('audit_type', sa.String(100), nullable=True),
        sa.Column('audit_date', sa.Date(), nullable=True),
        sa.Column('next_audit_date', sa.Date(), nullable=True),
        sa.Column('auditor', sa.String(255), nullable=True),
        sa.Column('scope', sa.Text(), nullable=True),
        sa.Column('findings', sa.Text(), nullable=True),
        sa.Column('corrective_actions', sa.Text(), nullable=True),
        sa.Column('result', sa.String(50), nullable=True),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        'approved_supplier_list',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('vendor_id', sa.Integer(), sa.ForeignKey('vendors.id'), unique=True, nullable=False),
        sa.Column('approval_status', sa.String(50), server_default='approved'),
        sa.Column('approved_date', sa.Date(), nullable=True),
        sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('scope', sa.Text(), nullable=True),
        sa.Column('certifications_required', sa.Text(), nullable=True),
        sa.Column('certifications_verified', sa.Boolean(), server_default='false'),
        sa.Column('last_review_date', sa.Date(), nullable=True),
        sa.Column('next_review_date', sa.Date(), nullable=True),
        sa.Column('review_frequency_months', sa.Integer(), server_default='12'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )


def _drop_table_if_exists(name: str) -> None:
    if _table_exists(name):
        op.drop_table(name)


def downgrade() -> None:
    tables = [
        'approved_supplier_list', 'supplier_audits', 'supplier_scorecards',
        'return_material_authorizations', 'customer_complaints',
        'spc_process_capabilities', 'spc_measurements', 'spc_control_limits', 'spc_characteristics',
        'eco_implementation_tasks', 'eco_approvals', 'engineering_change_orders',
        'skill_matrix', 'training_records', 'operator_certifications',
        'maintenance_logs', 'maintenance_work_orders', 'maintenance_schedules',
        'tool_usage_logs', 'tool_checkouts', 'tools',
        'cost_entries', 'job_costs',
        'downtime_events', 'downtime_reason_codes',
        'oee_targets', 'oee_records',
    ]
    for t in tables:
        _drop_table_if_exists(t)
