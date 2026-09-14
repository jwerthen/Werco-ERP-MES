"""Read-only code audit repros: all data exists only in fresh in-memory SQLite.
Run from backend/: PYTHONPATH=. <python> ../docs/codebase-audit-2026-09-13/evidence/backend_domain_repros.py
Injects one deliberate UNIQUE violation to exercise actual SQLAlchemy transaction failures.
"""
import os
os.environ.update(DATABASE_URL='sqlite:///:memory:', TEST_DATABASE_URL='sqlite:///:memory:', SECRET_KEY='audit-only-secret-key-abcdefghijklmnopqrstuvwxyz123456', REFRESH_TOKEN_SECRET_KEY='audit-only-refresh-key-abcdefghijklmnopqrstuvwxyz123456', ENVIRONMENT='test', SENTRY_DSN='')
import asyncio
import json
import logging
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sqlalchemy import Column, Integer, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import PendingRollbackError
from app.services.mrp_service import MRPService
from app.models.company import Company
from app.jobs import mrp_jobs, scheduling_jobs
from app.services import completion_cost_service as costs
logging.disable(logging.CRITICAL)

part = SimpleNamespace(safety_stock=0, lead_time_days=0, part_type='purchased')
db = Mock()
db.query.return_value.filter.return_value.first.return_value = part
service = MRPService(db, 1)
service.get_inventory_summary = lambda part_id: (0.0, 0.0, 0.0)
by_date = {(date.today()+timedelta(days=days)).isoformat(): 5.0 for days in (1,2)}
_, actions = service.calculate_shortages_and_actions({1: {'by_date': by_date}}, False)
assert [a.quantity for a in actions] == [5.0, 10.0]
print(json.dumps({'repro':'MRP_MULTI_DATE','total_demand':sum(by_date.values()),'recommended_quantities':[a.quantity for a in actions],'total_recommended':sum(a.quantity for a in actions)}))

AuditBase = declarative_base()
class Probe(AuditBase):
    __tablename__ = 'audit_backend_probe'
    id = Column(Integer, primary_key=True)

def setup():
    engine = create_engine('sqlite:///:memory:')
    Company.__table__.create(engine)
    AuditBase.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all([Company(id=1,name='One',slug='one'),Company(id=2,name='Two',slug='two'),Probe(id=1)])
    db.commit()
    return db

for module,run_name,helper_name in [(mrp_jobs,'run_mrp_task','_run_mrp_for_company'),(scheduling_jobs,'run_scheduling_task','_run_scheduling_for_company')]:
    db=setup(); seen=[]; failures=[]
    async def tenant_run(*,db,company_id,**kwargs):
        seen.append(company_id)
        try:
            if company_id == 1:
                db.add(Probe(id=1)); db.flush()
            db.execute(text('SELECT 1'))
            return {'company_id':company_id}
        except Exception as exc:
            failures.append({'company_id':company_id,'error':type(exc).__name__})
            raise
    with patch.object(module,'SessionLocal',return_value=db),patch.object(module,helper_name,tenant_run):
        result=asyncio.run(getattr(module,run_name)())
    assert failures == [{'company_id':1,'error':'IntegrityError'},{'company_id':2,'error':'PendingRollbackError'}]
    print(json.dumps({'repro':module.__name__,'tenants_attempted':seen,'failures':failures,'result':result}))

db=setup()
wo=SimpleNamespace(id=77,actual_hours=0.0,actual_cost=0.0)
def break_cost(db,*args):
    db.add(Probe(id=1)); db.flush()
with patch.object(costs,'is_labor_cost_rollup_enabled',return_value=True),patch.object(costs,'rollup_labor_hours_from_evidence'),patch.object(costs,'compute_and_store_actual_cost',break_cost):
    result=costs.apply_completion_cost_rollup(db,wo,company_id=1,user_id=1,audit=Mock())
try:
    db.execute(text('SELECT 1'))
    still_usable=True
except PendingRollbackError:
    still_usable=False
assert result is None and not still_usable
print(json.dumps({'repro':'COST_BEST_EFFORT','returned':result,'caller_session_usable':still_usable}))
db.rollback();db.close()
