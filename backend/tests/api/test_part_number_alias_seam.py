"""The part-number alias seam: retired numbers keep resolving, and can never be re-issued.

This is PR1 of the in-place part renumber. It ships the table and the resolver
BEFORE the verb that can create an alias row, and the ordering is a safety
requirement, not a preference: a renumber shipped before ``bom.py`` is
alias-aware would fork the catalog on the first spreadsheet re-import, because
``create_missing_parts`` defaults True and ``_ensure_part``'s miss path CREATES a
part. So these tests write ``PartNumberAlias`` rows directly -- there is no
endpoint that mints one yet, by design.

What each group locks, and why a naive version of the test would not:

* RESOLUTION -- a live number must ALWAYS win over an alias. Asserting only "the
  alias resolves" passes against a broken precedence order too, so the
  precedence test creates both and asserts WHICH one comes back.
* AVAILABILITY -- three distinct holders (live, soft-deleted, retired alias), each
  with its own message. The soft-deleted case is the one a live-rows-only probe
  silently misses: ``uq_parts_company_part_number`` has no partial predicate, so a
  tombstone still owns its number and the create used to fall through to a raw
  IntegrityError (a 500, since ``main.py`` has no handler).
* THE BOM FORK -- the highest-severity path in the whole feature. A re-import
  carrying a retired number must BIND to the renamed part, not mint a second one.
  The assertion counts ``Part`` rows, because "the import succeeded" passes just
  as happily when it succeeded by forking the catalog.
* CASE AND WHITESPACE -- mixed-case rows exist in production (``bom.py`` and
  ``po_upload.py`` construct ``Part(...)`` directly, bypassing
  ``PartBase.uppercase_part_number``), so a case-sensitive tier would miss on
  exactly the rows most likely to be renumbered.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.services.part_number_resolver import (
    find_part_number_conflict,
    resolve_part_by_number,
)

COMPANY_A = 1


def _make_part(
    db: Session,
    *,
    part_number: str,
    name: str = "Widget",
    part_type: str = "manufactured",
    is_deleted: bool = False,
    company_id: int = COMPANY_A,
) -> Part:
    part = Part(
        part_number=part_number,
        name=name,
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _retire(db: Session, *, part: Part, number: str, company_id: int = COMPANY_A) -> PartNumberAlias:
    """Write an alias row directly -- no endpoint mints one until PR2."""
    alias = PartNumberAlias(
        part_id=part.id,
        alias_number=number,
        alias_number_key=normalize_alias_key(number),
        reason="test renumber",
        company_id=company_id,
    )
    db.add(alias)
    db.commit()
    db.refresh(alias)
    return alias


@pytest.mark.api
@pytest.mark.requires_db
class TestAliasResolution:
    def test_retired_number_resolves_to_the_renamed_part(self, db_session: Session):
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        resolution = resolve_part_by_number(db_session, COMPANY_A, "OLD-123")
        assert resolution is not None
        assert resolution.part.id == part.id
        # The caller needs to know it arrived via a retired number so it can warn.
        assert resolution.matched_alias == "OLD-123"
        assert resolution.via_alias is True

    def test_a_live_number_always_beats_an_alias(self, db_session: Session):
        """Precedence, asserted by WHICH part comes back -- not merely that one does.

        If a retired number is ever re-issued to a genuinely different article,
        preferring the alias would resolve every old traveler and MTR to the WRONG
        physical part, and both answers look legitimate, so it is undetectable
        afterwards. ``find_part_number_conflict`` exists to make this state
        unreachable; this asserts the second line of defence behaves correctly if
        it ever is reached.
        """
        renamed = _make_part(db_session, part_number="NEW-456", name="Renamed")
        _retire(db_session, part=renamed, number="SHARED-1")
        live = _make_part(db_session, part_number="SHARED-1", name="A different article")

        resolution = resolve_part_by_number(db_session, COMPANY_A, "SHARED-1")
        assert resolution is not None
        assert resolution.part.id == live.id, "the LIVE part must win over a retired number"
        assert resolution.matched_alias is None

    @pytest.mark.parametrize("lookup", ["old-123", "  OLD-123  ", "Old-123"])
    def test_case_and_whitespace_insensitive(self, db_session: Session, lookup: str):
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        resolution = resolve_part_by_number(db_session, COMPANY_A, lookup)
        assert resolution is not None, f"{lookup!r} should resolve"
        assert resolution.part.id == part.id

    def test_live_lookup_is_also_case_insensitive(self, db_session: Session):
        """Mixed-case rows EXIST -- bom.py and po_upload.py bypass the uppercase validator."""
        part = _make_part(db_session, part_number="MixedCase-1")
        resolution = resolve_part_by_number(db_session, COMPANY_A, "mixedcase-1")
        assert resolution is not None and resolution.part.id == part.id

    def test_include_aliases_false_sees_only_live_numbers(self, db_session: Session):
        """The posture the laser sheet matcher and the fuzzy tier must use.

        An alias is a STALE SPEC: the sheet matcher parses thickness, size and alloy
        out of the number string, so resolving through a retired one would let a
        single physical part present two different material specs at once.
        """
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        assert resolve_part_by_number(db_session, COMPANY_A, "OLD-123", include_aliases=False) is None
        assert resolve_part_by_number(db_session, COMPANY_A, "NEW-456", include_aliases=False) is not None

    def test_alias_pointing_at_a_deleted_part_is_a_miss(self, db_session: Session):
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")
        part.is_deleted = True
        db_session.commit()

        assert resolve_part_by_number(db_session, COMPANY_A, "OLD-123") is None

    def test_aliases_are_tenant_scoped(self, db_session: Session):
        """Invariant 1. Another company's retired number must not resolve here."""
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        assert resolve_part_by_number(db_session, 999, "OLD-123") is None

    def test_blank_input_resolves_to_nothing(self, db_session: Session):
        assert resolve_part_by_number(db_session, COMPANY_A, "") is None
        assert resolve_part_by_number(db_session, COMPANY_A, "   ") is None


@pytest.mark.api
@pytest.mark.requires_db
class TestPartNumberAvailability:
    def test_free_number_has_no_conflict(self, db_session: Session):
        assert find_part_number_conflict(db_session, COMPANY_A, "BRAND-NEW") is None

    def test_live_part_conflicts(self, db_session: Session):
        _make_part(db_session, part_number="TAKEN-1")
        conflict = find_part_number_conflict(db_session, COMPANY_A, "TAKEN-1")
        assert conflict is not None and conflict.code == "LIVE_PART"

    def test_soft_deleted_part_conflicts_with_its_own_message(self, db_session: Session):
        """The case a live-rows-only probe misses, and the constraint then 500s on.

        ``uq_parts_company_part_number`` carries NO partial predicate, so a tombstone
        still owns its number -- invariant 3's named duplicate-probe exception. The
        message must be distinct from LIVE_PART because the remedy is different:
        restore that part, or pick another number.
        """
        _make_part(db_session, part_number="GONE-1", is_deleted=True)
        conflict = find_part_number_conflict(db_session, COMPANY_A, "GONE-1")
        assert conflict is not None
        assert conflict.code == "DELETED_PART"
        assert "deleted" in conflict.detail.lower()

    def test_retired_alias_conflicts(self, db_session: Session):
        """Re-issuing a retired number is the untraceable failure -- refuse it."""
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        conflict = find_part_number_conflict(db_session, COMPANY_A, "OLD-123")
        assert conflict is not None
        assert conflict.code == "RETIRED_ALIAS"
        assert conflict.part_id == part.id

    def test_excluding_part_id_lets_a_part_restate_its_own_number(self, db_session: Session):
        """Re-stating a value you already hold changes nothing and must not conflict."""
        part = _make_part(db_session, part_number="SAME-1")
        assert find_part_number_conflict(db_session, COMPANY_A, "SAME-1") is not None
        assert find_part_number_conflict(db_session, COMPANY_A, "SAME-1", excluding_part_id=part.id) is None

    def test_conflict_probe_is_case_insensitive(self, db_session: Session):
        _make_part(db_session, part_number="TAKEN-1")
        assert find_part_number_conflict(db_session, COMPANY_A, "taken-1") is not None

    def test_conflict_probe_is_tenant_scoped(self, db_session: Session):
        _make_part(db_session, part_number="TAKEN-1")
        assert find_part_number_conflict(db_session, 999, "TAKEN-1") is None


@pytest.mark.api
@pytest.mark.requires_db
class TestCreateDoorsRefuseRetiredNumbers:
    def test_create_part_refuses_a_retired_number(
        self, client: TestClient, auth_headers: dict, db_session: Session, sample_part_data: dict
    ):
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        response = client.post(
            "/api/v1/parts/", headers=auth_headers, json={**sample_part_data, "part_number": "OLD-123"}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "retired" in response.json()["detail"].lower()

    def test_create_part_refuses_a_soft_deleted_number_without_a_500(
        self, client: TestClient, auth_headers: dict, db_session: Session, sample_part_data: dict
    ):
        """Was a 500: the live-only probe passed, then the constraint raised."""
        _make_part(db_session, part_number="GONE-1", is_deleted=True)

        response = client.post(
            "/api/v1/parts/", headers=auth_headers, json={**sample_part_data, "part_number": "GONE-1"}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "deleted" in response.json()["detail"].lower()

    def test_create_material_refuses_a_retired_number(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """The materials door mints into the same table under the same constraint."""
        part = _make_part(db_session, part_number="RM-NEW", part_type="raw_material")
        _retire(db_session, part=part, number="RM-OLD")

        response = client.post(
            "/api/v1/materials/",
            headers=auth_headers,
            json={
                "part_number": "RM-OLD",
                "name": "Some stock",
                "part_type": "raw_material",
                "unit_of_measure": "each",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "retired" in response.json()["detail"].lower()


@pytest.mark.api
@pytest.mark.requires_db
class TestByNumberEndpoint:
    def test_resolves_a_retired_number_and_flags_it(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _make_part(db_session, part_number="NEW-456")
        _retire(db_session, part=part, number="OLD-123")

        response = client.get("/api/v1/parts/by-number/OLD-123", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["part_number"] == "NEW-456"
        # Header rather than a response field, so PartResponse keeps its shape and
        # an existing integration keeps working without a client change.
        assert response.headers.get("X-Resolved-From-Alias") == "OLD-123"

    def test_live_hit_sets_no_alias_header(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_part(db_session, part_number="NEW-456")
        response = client.get("/api/v1/parts/by-number/NEW-456", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        assert "X-Resolved-From-Alias" not in response.headers

    def test_unknown_number_still_404s(self, client: TestClient, auth_headers: dict):
        assert client.get("/api/v1/parts/by-number/NOPE-1", headers=auth_headers).status_code == 404


@pytest.mark.api
@pytest.mark.requires_db
class TestBomImportDoesNotForkTheCatalog:
    """The highest-severity path in the whole feature.

    ``create_missing_parts`` defaults True and ``_ensure_part``'s miss path CREATES
    a part. So before the alias tier, re-importing a BOM spreadsheet that still
    carried a renamed part's OLD number minted a SECOND part under the retired
    number -- forking the part master, splitting the stock across two identities,
    and binding the BOM line to the duplicate. It is partially visible (an audit row
    with ``source: bom_import`` and a "N parts created" count) but nothing says one
    of those creations duplicated an existing part.

    The assertions COUNT PART ROWS. "The import succeeded" passes just as happily
    when it succeeded by forking the catalog, so a naive test proves nothing here.
    """

    def _ensure(self, db, request_obj, *, number, create_missing=True, warnings=None):
        from app.api.endpoints.bom import _ensure_part
        from app.services.audit_service import AuditService

        return _ensure_part(
            db,
            number,
            "Some Component",
            "",
            "purchased",
            None,
            "each",
            create_missing,
            1,
            company_id=COMPANY_A,
            audit=AuditService(db, None, request_obj),
            warnings=warnings,
            line_label="Line 12",
        )

    def test_retired_number_binds_to_the_renamed_part_instead_of_creating_one(self, db_session: Session):
        renamed = _make_part(db_session, part_number="NEW-456", part_type="purchased")
        _retire(db_session, part=renamed, number="OLD-123")
        before = db_session.query(Part).filter(Part.company_id == COMPANY_A).count()

        warnings: list = []
        part, missing, was_created = self._ensure(db_session, None, number="OLD-123", warnings=warnings)

        assert part is not None and part.id == renamed.id, "must bind to the renamed part"
        assert missing is None
        assert was_created is False, "must NOT mint a second part"
        after = db_session.query(Part).filter(Part.company_id == COMPANY_A).count()
        assert after == before, f"catalog forked: {before} -> {after} parts"

    def test_the_bind_is_warned_not_silent(self, db_session: Session):
        """A controlled engineering document should say the spreadsheet is stale."""
        renamed = _make_part(db_session, part_number="NEW-456", part_type="purchased")
        _retire(db_session, part=renamed, number="OLD-123")

        warnings: list = []
        self._ensure(db_session, None, number="OLD-123", warnings=warnings)

        assert len(warnings) == 1
        assert "OLD-123" in warnings[0]
        assert "NEW-456" in warnings[0]
        assert "Line 12" in warnings[0]

    def test_alias_tier_runs_even_when_create_missing_is_off(self, db_session: Session):
        """``create_missing_parts=False`` means "do not INVENT parts", not "do not FIND parts".

        Below the early return, a stale line would take the ``missing`` marker, which
        the caller turns into a 400 that rolls back the ENTIRE import. Hoisting the
        alias tier above it converts a dead 200-line import into a live one.
        """
        renamed = _make_part(db_session, part_number="NEW-456", part_type="purchased")
        _retire(db_session, part=renamed, number="OLD-123")

        part, missing, was_created = self._ensure(db_session, None, number="OLD-123", create_missing=False)
        assert part is not None and part.id == renamed.id
        assert missing is None, "a resolvable retired number must not report as missing"
        assert was_created is False

    def test_an_unknown_number_still_creates_when_asked(self, db_session: Session):
        """The alias tier must not swallow the ordinary create path."""
        before = db_session.query(Part).filter(Part.company_id == COMPANY_A).count()
        part, missing, was_created = self._ensure(db_session, None, number="GENUINELY-NEW-1")
        assert part is not None and was_created is True and missing is None
        assert db_session.query(Part).filter(Part.company_id == COMPANY_A).count() == before + 1
