"""No real SMTP or Redis: transport phases, durable outcomes and tenant isolation."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from arq import Retry

import app.jobs.email_jobs as jobs
import app.services.email_service as module
from app.models.company import Company
from app.models.notification import Notification, NotificationLog
from app.services.email_service import EmailBeforeSubmissionFailure, EmailService, EmailSubmissionUnknown


@pytest.mark.parametrize('port', [587, 465])
def test_one_tls_negotiation_with_faithful_auto_upgrade_transport(monkeypatch, port):
    for name, value in {
        'SMTP_USER': 'test-user',
        'SMTP_PASSWORD': 'fake-only',
        'SMTP_HOST': 'smtp.example.test',
        'SMTP_FROM': 'erp@example.test',
        'SMTP_PORT': port,
    }.items():
        monkeypatch.setattr(module.settings, name, value)
    calls = []

    class SMTP:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.tls = kwargs.get('use_tls', False)
            calls.append(kwargs)

        async def __aenter__(self):
            if self.options.get('start_tls') is not False and not self.tls:
                self.tls = True  # Installed aiosmtplib upgrades opportunistically.
            return self

        async def __aexit__(self, *_args):
            pass

        async def starttls(self):
            assert not self.tls, 'Connection already using TLS'
            self.tls = True
            calls.append('starttls')

        async def login(self, *_args):
            assert self.tls

        async def send_message(self, message):
            assert self.tls and message['To'] == 'recipient@example.test'
            return {}, 'accepted'

    monkeypatch.setattr(module.aiosmtplib, 'SMTP', SMTP)
    assert asyncio.run(
        EmailService().send_email(
            to='recipient@example.test', subject='Test', template='notification', context={'title': 'Test'}
        )
    )
    assert calls[0]['start_tls'] is False
    assert calls.count('starttls') == (0 if port == 465 else 1)


@pytest.mark.parametrize('phase', ['connect', 'tls', 'submit', 'partial', 'quit'])
def test_before_submission_is_distinct_from_unknown_transport(monkeypatch, phase):
    monkeypatch.setattr(module.settings, 'SMTP_USER', 'test-user')
    monkeypatch.setattr(module.settings, 'SMTP_PASSWORD', 'fake-only')
    monkeypatch.setattr(module.settings, 'SMTP_PORT', 587)

    class SMTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            if phase == 'connect':
                raise OSError('fake connection failure')
            return self

        async def __aexit__(self, *_args):
            if phase == 'quit':
                raise OSError('fake quit failure')

        async def starttls(self):
            if phase == 'tls':
                raise OSError('fake TLS failure')

        async def login(self, *_args):
            pass

        async def send_message(self, _message):
            if phase == 'submit':
                raise OSError('fake DATA interruption')
            if phase == 'partial':
                return {'rejected@example.test': 'refused'}, 'partially accepted'
            return {}, 'accepted'

    monkeypatch.setattr(module.aiosmtplib, 'SMTP', SMTP)
    with pytest.raises(EmailBeforeSubmissionFailure if phase in {'connect', 'tls'} else EmailSubmissionUnknown):
        asyncio.run(EmailService().send_email(to='recipient@example.test', subject='Test', template='notification'))


@pytest.fixture
def queued_email(db_session, admin_user, monkeypatch):
    row = NotificationLog(
        company_id=1,
        user_id=admin_user.id,
        channel='email',
        event_type='wo.released',
        subject='Work order released',
        sent=False,
        provider_status='queued',
    )
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(jobs, 'SessionLocal', lambda: db_session)
    monkeypatch.setattr(db_session, 'close', lambda: None)
    return row, dict(
        to='untrusted-queued-address@example.test',
        subject=row.subject,
        body=None,
        template='notification',
        notification_log_id=row.id,
        company_id=1,
        user_id=admin_user.id,
    )


@pytest.mark.requires_db
def test_durable_claim_resolves_recipient_and_replays_without_smtp(db_session, admin_user, queued_email, monkeypatch):
    row, payload = queued_email
    expected_recipient = admin_user.email
    calls = []

    async def smtp(**kwargs):
        assert not db_session.in_transaction()
        assert kwargs['to'] == expected_recipient
        calls.append(kwargs)
        return True

    monkeypatch.setattr(jobs.email_service, 'send_email', smtp)
    assert asyncio.run(jobs.send_email_task(**payload)) == {'sent': True, 'status': 'accepted'}
    db_session.refresh(row)
    assert row.sent and row.provider_status == 'accepted' and row.provider_message_id
    replay = asyncio.run(jobs.send_email_task(**payload))
    assert replay['replayed'] and len(calls) == 1


@pytest.mark.requires_db
@pytest.mark.parametrize('outcome', ['failed', 'unknown', 'skipped'])
def test_terminal_outcome_is_visible_and_never_auto_resends(db_session, queued_email, monkeypatch, outcome):
    row, payload = queued_email
    effect = (
        EmailBeforeSubmissionFailure('fake')
        if outcome == 'failed'
        else EmailSubmissionUnknown('fake') if outcome == 'unknown' else None
    )
    smtp = AsyncMock(side_effect=effect, return_value=False)
    monkeypatch.setattr(jobs.email_service, 'send_email', smtp)
    result = asyncio.run(jobs.send_email_task(**payload, job_try=3))
    assert result['status'] == outcome and not result['sent']
    db_session.refresh(row)
    assert row.provider_status == outcome and not row.sent and row.error
    warning = db_session.query(Notification).filter_by(event_key='email.delivery_failed').one()
    assert warning.company_id == row.company_id and warning.user_id == row.user_id
    assert warning.link == f'/notifications?delivery={row.id}'
    assert asyncio.run(jobs.send_email_task(**payload))['replayed']
    assert smtp.await_count == 1
    assert db_session.query(Notification).filter_by(event_key='email.delivery_failed').count() == 1


@pytest.mark.requires_db
def test_only_definite_unsubmitted_failure_uses_bounded_retry(db_session, queued_email, monkeypatch):
    row, payload = queued_email
    smtp = AsyncMock(side_effect=EmailBeforeSubmissionFailure('fake'))
    monkeypatch.setattr(jobs.email_service, 'send_email', smtp)
    with pytest.raises(Retry):
        asyncio.run(jobs.send_email_task(**payload, job_try=1))
    db_session.refresh(row)
    assert row.provider_status == 'retrying' and not row.sent
    assert db_session.query(Notification).count() == 0
    smtp.side_effect = None
    smtp.return_value = True
    assert asyncio.run(jobs.send_email_task(**payload, job_try=2))['sent']
    assert smtp.await_count == 2


@pytest.mark.requires_db
def test_cross_company_job_does_not_send_or_modify_log(db_session, queued_email, monkeypatch):
    row, payload = queued_email
    db_session.add(Company(id=2, name='Other', slug='email-other'))
    db_session.commit()
    smtp = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs.email_service, 'send_email', smtp)
    result = asyncio.run(jobs.send_email_task(**dict(payload, company_id=2), job_try=3))
    assert result['reason'] == 'delivery_log_unavailable'
    db_session.refresh(row)
    assert row.provider_status == 'queued'
    smtp.assert_not_awaited()


@pytest.mark.requires_db
def test_enqueue_failure_is_recorded_and_warns_in_app(db_session, admin_user, monkeypatch):
    import app.services.notification_dispatch as dispatch

    monkeypatch.setattr(dispatch, '_dedup_reserve', AsyncMock(return_value=True))
    monkeypatch.setattr(dispatch, 'enqueue_job', AsyncMock(side_effect=OSError('Redis offline')))
    asyncio.run(
        dispatch.dispatch_direct(
            db_session,
            event_key='account.locked',
            company_id=1,
            recipients=[admin_user],
            title='Account locked',
            related_type='User',
            related_id=admin_user.id,
        )
    )
    log = db_session.query(NotificationLog).filter_by(channel='email').one()
    assert log.provider_status == 'failed' and not log.sent and 'could not be queued' in log.error
    assert db_session.query(Notification).filter_by(event_key='email.delivery_failed').count() == 1


@pytest.mark.requires_db
def test_log_filters_exclude_pending_include_stalled_and_isolate_company(
    client, admin_headers, operator_headers, db_session, admin_user
):
    from datetime import datetime, timedelta

    db_session.add(Company(id=2, name='Other', slug='logs-other'))
    rows = []
    for status, company, age in [
        ('failed', 1, 0),
        ('unknown', 1, 0),
        ('queued', 1, 0),
        ('retrying', 1, 0),
        ('sending', 1, 0),
        ('sending', 1, 20),
        ('failed', 2, 0),
    ]:
        row = NotificationLog(
            company_id=company,
            user_id=admin_user.id,
            channel='email',
            event_type='wo.released',
            sent=False,
            provider_status=status,
            sent_at=datetime.utcnow() - timedelta(minutes=age),
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    response = client.get(
        '/api/v1/notifications/logs',
        headers=admin_headers,
        params={'channel': 'email', 'status': 'failed', 'mine_only': False},
    )
    assert response.status_code == 200
    assert {row['id'] for row in response.json()} == {rows[0].id, rows[1].id, rows[5].id}
    assert (
        client.get('/api/v1/notifications/logs', headers=admin_headers, params={'delivery_id': rows[-1].id}).json()
        == []
    )
    assert (
        client.get(
            '/api/v1/notifications/logs',
            headers=operator_headers,
            params={'delivery_id': rows[0].id, 'mine_only': False},
        ).json()
        == []
    )


@pytest.mark.requires_db
@pytest.mark.parametrize('outcome', ['accepted', 'skipped', 'unknown'])
def test_digest_marks_only_accepted_or_uncertain_attempts_processed(
    db_session, admin_user, queued_email, monkeypatch, outcome
):
    from datetime import datetime

    from app.models.notification import DigestQueue, NotificationPreference

    db_session.add(
        NotificationPreference(
            company_id=1, user_id=admin_user.id, preferences={}, digest_enabled=True, digest_frequency='DAILY'
        )
    )
    item = DigestQueue(
        company_id=1,
        user_id=admin_user.id,
        event_type='WO_LATE',
        event_data={'wo_number': 'WO-TEST', 'days_late': 2},
        processed=False,
        created_at=datetime.utcnow(),
    )
    db_session.add(item)
    db_session.commit()
    calls = []

    async def smtp(**kwargs):
        assert not db_session.in_transaction()
        assert kwargs['context']['user']['full_name']
        calls.append(kwargs)
        if outcome == 'unknown':
            raise EmailSubmissionUnknown('fake interruption')
        return outcome == 'accepted'

    monkeypatch.setattr(jobs.email_service, 'send_email', smtp)
    result = asyncio.run(jobs.send_daily_digest_task())
    assert result['digests_sent'] == (1 if outcome == 'accepted' else 0)
    db_session.refresh(item)
    assert item.processed is (outcome in {'accepted', 'unknown'})
    log = db_session.query(NotificationLog).filter_by(event_type='email.daily_digest').one()
    assert log.provider_status == outcome
    assert len(calls) == 1


@pytest.mark.requires_db
def test_digest_recovers_prior_sending_claim_without_repeating_old_items(
    db_session, admin_user, queued_email, monkeypatch
):
    import json
    from datetime import datetime

    from app.models.notification import DigestQueue, NotificationPreference

    db_session.add(
        NotificationPreference(
            company_id=1, user_id=admin_user.id, preferences={}, digest_enabled=True, digest_frequency='DAILY'
        )
    )
    old = DigestQueue(
        company_id=1,
        user_id=admin_user.id,
        event_type='WO_LATE',
        event_data={'wo_number': 'OLD'},
        processed=False,
        created_at=datetime.utcnow(),
    )
    new = DigestQueue(
        company_id=1,
        user_id=admin_user.id,
        event_type='WO_LATE',
        event_data={'wo_number': 'NEW'},
        processed=False,
        created_at=datetime.utcnow(),
    )
    db_session.add_all([old, new])
    db_session.flush()
    db_session.add(
        NotificationLog(
            company_id=1,
            user_id=admin_user.id,
            channel='email',
            event_type='email.daily_digest',
            provider_status='sending',
            sent=False,
            related_type='digest_queue',
            related_id=old.id,
            body=json.dumps({'digest_item_ids': [old.id]}),
        )
    )
    db_session.commit()
    smtp = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs.email_service, 'send_email', smtp)
    assert asyncio.run(jobs.send_daily_digest_task())['digests_sent'] == 1
    events = smtp.await_args.kwargs['context']['events']['WO_LATE']
    assert events == [{'wo_number': 'NEW'}]
    db_session.refresh(old)
    db_session.refresh(new)
    assert old.processed and new.processed
