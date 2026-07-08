"""Phase 3 tests: verification report + finalize gate."""

from __future__ import annotations

import pytest

from app.models.quote import Quote
from app.models.quote_config import MaterialCategory, QuoteMaterial
from app.models.rfq_quote import RfqPackage
from app.services.estimate_workbench_service import (
    FinalizeBlockedError,
    build_verification_report,
    create_blank_estimate,
    finalize_estimate,
    save_estimate_tree,
)


@pytest.fixture
def rfq_package(db_session):
    pkg = RfqPackage(
        rfq_number="RFQ-EW-P3-001",
        customer_name="Verify Customer",
        status="uploaded",
        company_id=1,
    )
    db_session.add(pkg)
    db_session.commit()
    db_session.refresh(pkg)
    return pkg


@pytest.fixture
def material(db_session):
    m = QuoteMaterial(
        name="A36 Mild Steel",
        category=MaterialCategory.STEEL,
        stock_price_per_pound=0.90,
        density_lb_per_cubic_inch=0.284,
        is_active=True,
        company_id=1,
    )
    db_session.add(m)
    db_session.commit()
    return m


def _save_with_fab(db_session, estimate, *, confidence="review", note=None, buyout=None):
    payload = {
        "assemblies": [
            {
                "name": "Asm-1",
                "fab_lines": [
                    {
                        "detail_name": "Panel",
                        "material": "A36 Mild Steel",
                        "qty": 2,
                        "thickness_in": 0.075,
                        "width_in": 10,
                        "length_in": 10,
                        "cut_length_in": 40,
                        "bend_count": 2,
                        "confidence": confidence,
                        "verification_note": note,
                    }
                ],
                "buyout_lines": buyout or [],
            }
        ],
        "machined_parts": [],
    }
    return save_estimate_tree(
        db_session,
        estimate,
        payload,
        expected_version=estimate.version,
        company_id=1,
    )


class TestVerificationReport:
    def test_review_items_block_finalize(self, db_session, rfq_package, material):
        estimate = create_blank_estimate(db_session, rfq_package_id=rfq_package.id, company_id=1, user_id=None)
        saved = _save_with_fab(db_session, estimate, confidence="review")
        report = build_verification_report(saved)
        assert report["can_finalize"] is False
        assert report["review_count"] >= 1
        assert report["status"] == "needs_review"
        assert report["banner"]
        assert any(a["category"] == "fab" for a in report["priority_actions"])

    def test_confirmed_clears_gate(self, db_session, rfq_package, material):
        estimate = create_blank_estimate(db_session, rfq_package_id=rfq_package.id, company_id=1, user_id=None)
        saved = _save_with_fab(db_session, estimate, confidence="confirmed", note="Checked against drawing")
        report = build_verification_report(saved)
        assert report["can_finalize"] is True
        assert report["review_count"] == 0
        assert report["status"] == "ready_to_send"
        assert report["banner"] is None

    def test_buyout_review_without_note_is_blocker(self, db_session, rfq_package, material):
        estimate = create_blank_estimate(db_session, rfq_package_id=rfq_package.id, company_id=1, user_id=None)
        saved = _save_with_fab(
            db_session,
            estimate,
            confidence="confirmed",
            note="ok",
            buyout=[
                {
                    "description": "Custom bracket",
                    "qty": 1,
                    "unit_cost": 0,
                    "confidence": "review",
                    # no note / price_source
                }
            ],
        )
        report = build_verification_report(saved)
        assert report["can_finalize"] is False
        assert any(b.get("blocker_type") == "missing_note" for b in report["blockers"])


class TestFinalize:
    def test_finalize_blocked_when_review(self, db_session, rfq_package, material):
        estimate = create_blank_estimate(db_session, rfq_package_id=rfq_package.id, company_id=1, user_id=None)
        saved = _save_with_fab(db_session, estimate, confidence="review")
        with pytest.raises(FinalizeBlockedError) as exc:
            finalize_estimate(db_session, saved, company_id=1, user_id=None)
        assert len(exc.value.blockers) >= 1

    def test_finalize_creates_quote(self, db_session, rfq_package, material):
        estimate = create_blank_estimate(db_session, rfq_package_id=rfq_package.id, company_id=1, user_id=None)
        saved = _save_with_fab(db_session, estimate, confidence="confirmed", note="Verified")
        result = finalize_estimate(db_session, saved, company_id=1, user_id=None)
        assert result["quote_id"]
        assert result["quote_number"].startswith("QTE-")
        assert result["grand_total"] > 0
        quote = db_session.query(Quote).filter(Quote.id == result["quote_id"]).first()
        assert quote is not None
        assert quote.customer_name == "Verify Customer"
        assert len(quote.lines) >= 1
        from app.services.estimate_workbench_service import get_estimate_tree

        refreshed = get_estimate_tree(db_session, saved.id, 1)
        assert refreshed.internal_breakdown.get("rate_snapshot")
        assert refreshed.internal_breakdown.get("finalized_at")
        assert refreshed.quote_id == result["quote_id"]
