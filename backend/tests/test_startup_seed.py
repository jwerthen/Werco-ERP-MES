import app.db.database as database
from app.main import seed_quote_config_if_needed
from app.models.company import Company
from app.models.quote_config import (
    QuoteFinish,
    QuoteMachine,
    QuoteMaterial,
    QuoteSettings,
)


def _run_seed(monkeypatch, db_session):
    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    seed_quote_config_if_needed()


def _quote_config_counts(db_session, company_id: int) -> dict[str, int]:
    return {
        "materials": db_session.query(QuoteMaterial).filter(QuoteMaterial.company_id == company_id).count(),
        "machines": db_session.query(QuoteMachine).filter(QuoteMachine.company_id == company_id).count(),
        "finishes": db_session.query(QuoteFinish).filter(QuoteFinish.company_id == company_id).count(),
        "settings": db_session.query(QuoteSettings).filter(QuoteSettings.company_id == company_id).count(),
    }


def test_seed_quote_config_is_tenant_scoped_and_idempotent(monkeypatch, db_session):
    db_session.add(Company(id=2, name="Other Manufacturing", slug="other", is_active=True))
    db_session.commit()

    _run_seed(monkeypatch, db_session)

    expected_counts = {
        "materials": 6,
        "machines": 5,
        "finishes": 6,
        "settings": 10,
    }
    assert _quote_config_counts(db_session, 1) == expected_counts
    assert _quote_config_counts(db_session, 2) == expected_counts

    assert db_session.query(QuoteMaterial).filter(QuoteMaterial.company_id.is_(None)).count() == 0
    assert db_session.query(QuoteMachine).filter(QuoteMachine.company_id.is_(None)).count() == 0
    assert db_session.query(QuoteFinish).filter(QuoteFinish.company_id.is_(None)).count() == 0
    assert db_session.query(QuoteSettings).filter(QuoteSettings.company_id.is_(None)).count() == 0

    _run_seed(monkeypatch, db_session)

    assert _quote_config_counts(db_session, 1) == expected_counts
    assert _quote_config_counts(db_session, 2) == expected_counts
