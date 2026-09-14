import os
os.environ.update(TEST_DATABASE_URL='sqlite:///file:werco_security_review?mode=memory&cache=shared&uri=true', DATABASE_URL='sqlite:///file:werco_security_review?mode=memory&cache=shared&uri=true', ENVIRONMENT='test', RATE_LIMIT_ENABLED='false', STORAGE_BACKEND='local')
import runpy
c=runpy.run_path('tests/conftest.py')
from fastapi.testclient import TestClient
from app.models.user import User,UserRole
from app.models.company import Company
from app.models.part import Part,PartType
from app.models.quality import FirstArticleInspection,FAICharacteristic
from app.models.audit_log import AuditLog
from app.core.security import create_access_token
from app.db.database import get_db
from app.api.endpoints import documents
from unittest.mock import patch
Base,engine=c['Base'],c['engine']
assert engine.url.get_backend_name()=='sqlite'
Base.metadata.create_all(bind=engine)
s=c['TestingSessionLocal']()
s.add_all([Company(id=1,name='Audit Tenant A',slug='audit-a'),Company(id=2,name='Audit Tenant B',slug='audit-b')]);s.flush()
admin=User(email='tenant.admin@example.com',employee_id='AUDIT-1',first_name='Tenant',last_name='Admin',role=UserRole.ADMIN,is_active=True,company_id=1,hashed_password=c['TEST_PASSWORD_HASH'])
viewer=User(email='tenant.viewer@example.com',employee_id='AUDIT-2',first_name='Tenant',last_name='Viewer',role=UserRole.VIEWER,is_active=True,company_id=1,hashed_password=c['TEST_PASSWORD_HASH'])
s.add_all([admin,viewer]);s.flush()
p1=Part(company_id=1,part_number='AUDIT-A',name='Audit A',part_type=PartType.MANUFACTURED)
p2=Part(company_id=2,part_number='AUDIT-B',name='Audit B',part_type=PartType.MANUFACTURED)
s.add_all([p1,p2]);s.flush()
f1=FirstArticleInspection(company_id=1,fai_number='FAI-A',part_id=p1.id,total_characteristics=1,characteristics_failed=1)
f2=FirstArticleInspection(company_id=2,fai_number='FAI-B',part_id=p2.id,total_characteristics=1,characteristics_failed=1)
s.add_all([f1,f2]);s.flush()
ch=FAICharacteristic(company_id=2,fai_id=f2.id,char_number=1,characteristic='Critical evidence',is_conforming=False)
s.add(ch);s.commit()
app=c['app']
def session():yield s
app.dependency_overrides[get_db]=session
client=TestClient(app,raise_server_exceptions=False)
def hdr(u,scope=None):return {'Authorization':'Bearer '+create_access_token(u.id,company_id=u.company_id,scope=scope)}
ha,hv=hdr(admin),hdr(viewer)
payload={'email':'escape@example.com','employee_id':'AUDIT-3','first_name':'Scope','last_name':'Probe','role':'platform_admin','password':c['TEST_PASSWORD']}
print('1 register:',client.post('/api/v1/users/',json=payload,headers=ha).status_code,client.post('/api/v1/auth/register',json=payload,headers=ha).status_code,flush=True)
login=client.post('/api/v1/auth/login',data={'username':payload['email'],'password':payload['password']})
hp={'Authorization':'Bearer '+login.json()['access_token']}
print('1 other-tenant read:',client.get('/api/v1/platform/companies/2',headers=hp).status_code,flush=True)
before=s.query(AuditLog).count();r=client.put('/api/v1/platform/companies/2',json={'is_active':False},headers=hp)
print('2 deactivate:',r.status_code,'is_active',s.get(Company,2).is_active,'audit delta',s.query(AuditLog).count()-before,flush=True)
before=s.query(AuditLog).count();r=client.delete(f'/api/v1/quality/fai/{f2.id}/characteristics/{ch.id}',headers=hv)
print('3 cross-tenant delete:',r.status_code,'characteristic present',s.query(FAICharacteristic).filter_by(id=ch.id).count(),'other-tenant counts',f2.total_characteristics,f2.characteristics_failed,'audit delta',s.query(AuditLog).count()-before,flush=True)
before=s.query(AuditLog).count();r=client.put(f'/api/v1/quality/fai/{f1.id}',json={'status':'passed','version':0},headers=hv)
print('4 viewer FAI approval:',r.status_code,'status',f1.status,'approved by',f1.approved_by,'viewer',viewer.id,'audit delta',s.query(AuditLog).count()-before,flush=True)
r=client.post(f'/api/v1/quality/fai/{f1.id}/characteristics',json={'char_number':2,'characteristic':'New dimension'},headers=ha)
print('5 add characteristic:',r.status_code,r.text[:250],flush=True)
s.rollback()
viewer=s.get(User,viewer.id)
old_token=create_access_token(viewer.id,company_id=1)
viewer.is_active=False;s.commit()
print('6 disabled HTTP:',client.get('/api/v1/users/me',headers={'Authorization':'Bearer '+old_token}).status_code,flush=True)
with client.websocket_connect('/api/v1/ws/updates?token='+old_token) as ws:
 print('6 disabled WebSocket:',ws.receive_json()['type'],flush=True)
viewer.is_active=True;s.commit()
kiosk=create_access_token(viewer.id,company_id=1,scope='kiosk')
print('6 kiosk HTTP general:',client.get('/api/v1/users/me',headers={'Authorization':'Bearer '+kiosk}).status_code,flush=True)
with client.websocket_connect('/api/v1/ws/updates?token='+kiosk) as ws:
 print('6 kiosk general WebSocket:',ws.receive_json()['type'],flush=True)
class FakeStorage:
 is_remote=True
 def save(self,data,*,key):return 's3://offline-audit/'+key
before=s.query(AuditLog).count()
with patch.object(documents,'get_storage',return_value=FakeStorage()):
 r=client.post('/api/v1/documents/upload',headers=hv,data={'title':'Viewer instruction','document_type':'work_instruction'},files={'file':('audit.pdf',b'%PDF-1.4\nAudit only','application/pdf')})
print('7 Viewer document upload:',r.status_code,'status',r.json().get('status'),'audit delta',s.query(AuditLog).count()-before,flush=True)
from app.models.job_costing import JobCost,CostEntry,CostEntryType
from app.models.work_order import WorkOrder
from datetime import date
wo=WorkOrder(company_id=2,work_order_number='AUDIT-WO-B',part_id=p2.id,quantity_ordered=1,customer_name='Private tenant B customer')
s.add(wo);s.flush()
jc=JobCost(company_id=2,work_order_id=wo.id,revenue=10000,notes='Private margin notes')
s.add(jc);s.flush()
ce=CostEntry(company_id=2,job_cost_id=jc.id,entry_type=CostEntryType.MATERIAL,description='Confidential cost',entry_date=date.today(),total_cost=500,unit_cost=500)
s.add(ce);s.commit()
for path in [f'/api/v1/job-costs/{jc.id}/entries',f'/api/v1/job-costs/{jc.id}/variance-report']:
 r=client.get(path,headers=hv)
 print('8 cross-tenant read',path,r.status_code,r.text[:100],flush=True)
r=client.put(f'/api/v1/job-costs/{jc.id}',headers=hv,json={'revenue':1,'notes':'Changed from tenant A'})
print('8 cross-tenant mutate',r.status_code,'persisted revenue',jc.revenue,flush=True)
r=client.delete(f'/api/v1/job-costs/{jc.id}/entries/{ce.id}',headers=hv)
print('8 cross-tenant cost delete',r.status_code,'remaining',s.query(CostEntry).filter_by(id=ce.id).count(),flush=True)
client.close();s.close();app.dependency_overrides.clear()
