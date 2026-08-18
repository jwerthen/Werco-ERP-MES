"""``services/user_identity`` -- the one shape, pinned so the three seams cannot drift.

This module exists because the same two derivations were implemented twice each in
slightly different words (the user CSV importer, the legacy login-email repair) and a
third copy was about to be written for public registration. Extracting them only helps if
the extraction is byte-faithful, so these tests assert the exact strings the previous
copies produced -- the sanitizer character classes, the ``emp-`` prefix, the ``employee``
and ``user`` all-punctuation fallbacks, the case rule (dropped one way, preserved the
other) and each walk's suffix sequence.

**THE TWO WALKS SUFFIX DIFFERENTLY, and §7 is where that is pinned.** The mint steps
``-2``, ``-3``, ... because its collisions are whole EMAIL ADDRESSES, where a digit means
nothing to any resolver. The badge walk steps ``-b``, ``-c``, ... ``-z``, ``-aa``, ...
because a digit inside a BADGE is a coordinate: ``auth._normalize_employee_id`` reads a
badge as its last four digits, so ``jmw-2`` IS badge ``0002``. On a shop whose badges run
contiguously from 0001 every numeric candidate collided, the walk exhausted its cap, and a
legitimate email-only signup was dropped behind the uniform "submitted for approval" body
having created nothing. Letters carry no digits, so they cannot land in that keyspace at
all -- which is why §7 asserts the normalization of the WHOLE candidate sequence and not
one sample of it.

The two functions run in OPPOSITE directions and their sanitizers are deliberately
DIFFERENT, which is the detail a "tidy-up" would most likely destroy:

* ``synthetic_email_for_employee_id`` builds an email LOCAL PART, so it lowercases and
  keeps ``[a-z0-9._-]`` -- ``.`` is legal there.
* ``employee_id_from_email`` builds a BADGE, so it preserves case and keeps
  ``[A-Za-z0-9\\-_]`` -- ``.`` is NOT legal there, which is why ``john.doe`` becomes
  ``johndoe``.

Both are pure given the ``is_taken`` predicate: dedup SCOPE belongs to the caller (the
importer holds a preloaded per-company set, the two auth seams probe the DB install-wide),
so these tests drive the predicate directly instead of standing a database up. That is
also the only way to assert the suffix WALK rather than just its first step -- the
recorded probe order is the assertion.

§6 covers the other property both walks share: they are BOUNDED. ``is_taken`` is the
caller's, so neither walk may assume it is a function of the candidate -- public
registration's provably is not -- and an unbounded walk on an unauthenticated route is an
infinite loop that queries the database once per turn. Those tests are written so a build
with the cap removed FAILS rather than hangs.

§8 holds the walk to its own DOCUMENTATION. The cap is illustrated by hand in the
exception's docstring and in ``docs/API.md`` -- "100 accounts holding ``jmw``, ``jmw-b`` ...
``jmw-cv``" -- and both spelled it one letter string too far. Nothing executes a docstring,
so a test that reads the prose is the only thing that can hold the two together; §8 asserts
that every ``jmw-<letters>`` example in either source is a candidate the walk really offers,
and that the cap example is the LAST one.

``is_synthetic_email`` (§4) is the READ side of the first derivation, and it lives here for
the same reason: it answers "did we mint this address ourselves, so nothing can be
delivered to it?", and the notification dispatcher now refuses to record a send against a
True answer. A reader that drifts from the minter is a silent regression in BOTH
directions -- too narrow and the dispatcher logs deliveries that could only bounce, too
wide and it stops mailing people whose addresses are real. §4 pins both edges.
"""

import pathlib
import re

import pytest

import app.services.user_identity as user_identity_module
from app.api.endpoints.auth import _normalize_employee_id
from app.api.endpoints.users import _generated_email
from app.services.user_identity import (
    LEGACY_RESERVED_EMAIL_DOMAIN,
    MAX_EMPLOYEE_ID_LENGTH,
    MAX_IDENTIFIER_CANDIDATES,
    SYNTHETIC_EMAIL_DOMAIN,
    IdentifierDerivationExhausted,
    employee_id_from_email,
    is_synthetic_email,
    synthetic_email_for_employee_id,
)

pytestmark = pytest.mark.unit

NEVER_TAKEN = {}


def _never(candidate: str) -> bool:
    return False


def _taken(*values: str):
    """A predicate over a fixed taken-set that RECORDS what it was asked, in order."""
    taken = set(values)
    probed: list = []

    def is_taken(candidate: str) -> bool:
        probed.append(candidate)
        return candidate in taken

    return is_taken, probed


# ===========================================================================
# 1. email <- badge
# ===========================================================================


def test_the_synthetic_domain_is_the_one_login_recognises():
    """Not a free choice. ``_find_user_by_auth_email`` hard-codes ``@users.werco.com`` for
    its legacy-address fallback, so this constant and that literal are one contract; a
    different domain here mints addresses that route differently at sign-in."""
    assert SYNTHETIC_EMAIL_DOMAIN == "users.werco.com"


@pytest.mark.parametrize(
    "employee_id,expected",
    [
        ("EMP-0777", "emp-emp-0777@users.werco.com"),  # lowercased, prefix added
        ("emp-0777", "emp-emp-0777@users.werco.com"),  # case is dropped, so both map alike
        ("0042", "emp-0042@users.werco.com"),
        ("keep_me.now-1", "emp-keep_me.now-1@users.werco.com"),  # . _ - all legal in a local part
        ("Badge #42!", "emp-badge42@users.werco.com"),  # space, # and ! are stripped, not encoded
        ("ünïcode", "emp-ncode@users.werco.com"),  # non-ASCII is dropped, never transliterated
    ],
)
def test_the_minted_address_shape(employee_id: str, expected: str):
    assert synthetic_email_for_employee_id(employee_id, _never) == expected


@pytest.mark.parametrize("employee_id", ["", "###", "   ", "@@@", None])
def test_a_badge_that_sanitizes_to_nothing_falls_back_to_employee(employee_id):
    """``User.email`` is NOT NULL and an empty local part is not a parseable address, so a
    badge of pure punctuation must still produce something insertable. ``None`` is in the
    set because two of the three callers pass a column that is typed optional."""
    assert synthetic_email_for_employee_id(employee_id, _never) == "emp-employee@users.werco.com"


def test_the_minted_address_walks_the_suffix_until_it_finds_a_free_one():
    """The sequence, not just its first step: base, then ``-2``, ``-3``, ... with the
    suffix BEFORE the ``@``. Recording the probes is what pins that the walk is ordered
    and gapless -- a rewrite that started at ``-1``, or that appended after the domain,
    would still return "something unused" and pass a weaker assertion."""
    is_taken, probed = _taken(
        "emp-emp-0777@users.werco.com",
        "emp-emp-0777-2@users.werco.com",
        "emp-emp-0777-3@users.werco.com",
    )

    assert synthetic_email_for_employee_id("EMP-0777", is_taken) == "emp-emp-0777-4@users.werco.com"
    assert probed == [
        "emp-emp-0777@users.werco.com",
        "emp-emp-0777-2@users.werco.com",
        "emp-emp-0777-3@users.werco.com",
        "emp-emp-0777-4@users.werco.com",
    ]


def test_the_csv_importer_seam_still_produces_the_identical_string():
    """The delegation, checked rather than assumed.

    ``api/endpoints/users.py::_generated_email`` is now a one-line pass-through that adds
    only its per-company set membership test. If it ever grows a second implementation
    again, imported rows and registered rows start minting different addresses for the
    same badge -- which is invisible until two of them collide.
    """
    assert _generated_email("EMP-0777", set()) == synthetic_email_for_employee_id("EMP-0777", _never)
    assert _generated_email("###", set()) == "emp-employee@users.werco.com"
    assert _generated_email("EMP-0777", {"emp-emp-0777@users.werco.com"}) == "emp-emp-0777-2@users.werco.com"


# ===========================================================================
# 2. badge <- email
# ===========================================================================


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jmw@wercomfg.com", "jmw"),
        ("JMW@wercomfg.com", "JMW"),  # CASE IS PRESERVED here -- opposite of the mint
        ("john.doe+tag@wercomfg.com", "johndoetag"),  # . and + are not badge characters
        ("keep-me_01@wercomfg.com", "keep-me_01"),
        ("bare-local-part", "bare-local-part"),  # no @ at all: the whole string is the local part
    ],
)
def test_the_derived_badge_shape(email: str, expected: str):
    assert employee_id_from_email(email, _never) == expected


@pytest.mark.parametrize("email", ["+@example.com", "@example.com", "", None])
def test_a_local_part_that_sanitizes_to_nothing_falls_back_to_user(email):
    """``+@example.com`` is a legal address whose local part strips to nothing. The old
    inline copy wrote ``employee_id = ""`` here -- a row that collides with the next such
    registration on a UNIQUE constraint and matches nothing at badge login."""
    assert employee_id_from_email(email, _never) == "user"


def test_a_local_part_of_bare_punctuation_falls_back_to_user_on_the_PRIMARY_path():
    """``----@x.co`` -> ``user``. The defect this pins is one an EMPTINESS test cannot see.

    The badge sanitizer KEEPS ``-`` and ``_`` -- they are legitimate INSIDE a badge -- so a
    local part made of nothing else survives it WHOLE. ``----`` is not the empty string, so
    the ``if not sanitized`` fallback this path used to carry passed it straight through and
    the registrant was issued a badge literally reading ``----``: nothing a scanner can
    produce, nothing an admin can search for, and a UNIQUE-constraint collision with the
    next such signup.

    The twin below covers the DIGIT-STRIPPED path, which was corrected one round earlier and
    already tested for an alphanumeric. THE DRIFT BETWEEN THE TWO IS THE FINDING -- one
    branch fixed, its neighbour left on the weaker test -- so the two tests are written
    adjacently and phrased identically rather than filed under the sections they belong to.
    """
    assert employee_id_from_email("a-_-@x.co", _never) == "a-_-", (
        "precondition: the sanitizer really does keep ``-`` and ``_``, so every local part "
        "below survives it non-empty and an emptiness test would have accepted them all"
    )

    assert employee_id_from_email("----@x.co", _never) == "user"
    assert employee_id_from_email("_@x.co", _never) == "user"
    assert employee_id_from_email("-_-@x.co", _never) == "user"


def test_a_digit_strip_that_leaves_bare_punctuation_falls_back_to_user_on_the_STRIPPED_path():
    """The twin, on the far side of the branch -- same rule, reached a different way.

    Here the base carries alphanumerics, so it IS offered (``2024-05``), comes back taken,
    and the walk drops to the digits-stripped spelling to leave the four-digit badge
    keyspace (§7). That strip leaves ``-``: non-empty, so an emptiness test accepts it, and
    the registrant is handed the same unusable badge the primary path used to hand out.

    ``0339`` -> ``""`` is the case §7 already pins. This one is deliberately the case an
    emptiness test CANNOT catch, which is why it is a separate assertion rather than another
    row in that parametrize.
    """
    is_taken, probed = _taken("2024-05")
    assert employee_id_from_email("2024-05@x.co", is_taken) == "user"
    assert probed == ["2024-05", "user"], "the fallback base is offered directly, not suffixed first"

    underscore_taken, underscore_probed = _taken("_2024")
    assert employee_id_from_email("_2024@x.co", underscore_taken) == "user"
    assert underscore_probed == ["_2024", "user"]


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jmw@wercomfg.com", "jmw"),
        ("JMW@wercomfg.com", "JMW"),  # case still preserved
        ("0339@x.co", "0339"),  # an all-DIGIT badge is a perfectly ordinary badge
        ("jw2024@x.co", "jw2024"),
    ],
)
def test_the_alphanumeric_fallback_left_ordinary_spellings_byte_identical(email: str, expected: str):
    """The whole constraint on the two fixes above: fire only where there is nothing to keep.

    Widening "empty" to "no alphanumeric character" changes a CONDITION, and the cheap way
    to get that wrong is to widen it too far. A rule reading "no LETTER" would rewrite badge
    ``0339`` to ``user`` and silently detach an operator from the number printed on their
    card -- a worse outcome than the one being fixed, and one nothing in the response would
    report. Case, a pure-digit badge and a digit-bearing mixed spelling are pinned
    separately because they are the three shapes such a rule eats first.
    """
    assert employee_id_from_email(email, _never) == expected


def test_the_derived_badge_walks_the_suffix_until_it_finds_a_free_one():
    """Same walk, no ``@`` to sit in front of -- but the suffix is a LETTER.

    ``-a`` is never offered: the BARE BASE occupies that slot, so the sequence starts at
    its second element, ``-b``. The letters are not cosmetic -- see §7 for the property
    they buy and for the numeric spelling this replaced, which could not converge at all
    on a shop with contiguous badge numbers.
    """
    is_taken, probed = _taken("jmw", "jmw-b")

    assert employee_id_from_email("jmw@wercomfg.com", is_taken) == "jmw-c"
    assert probed == ["jmw", "jmw-b", "jmw-c"]
    assert "jmw-a" not in probed, "the bare base already occupies that slot"


def test_the_two_directions_are_not_inverses_and_are_not_meant_to_be():
    """Stated so nobody "fixes" the asymmetry.

    Round-tripping a badge through the mint and back does NOT return the badge: the mint
    lowercases and prefixes, so ``EMP-0777`` comes back as ``emp-emp-0777``. Each function
    exists to fill a NOT NULL column for a user who genuinely has only the other
    identifier -- neither is a decoder, and no caller may treat them as one.
    """
    minted = synthetic_email_for_employee_id("EMP-0777", _never)
    assert employee_id_from_email(minted, _never) == "emp-emp-0777"


# ===========================================================================
# 3. The badge derivation is capped to the COLUMN, suffix included
# ===========================================================================
#
# ``users.employee_id`` is ``String(50)``; an email local part may legally be 64
# characters (RFC 5321). Uncapped, ``employee_id_from_email`` therefore hands the
# insert a value the column cannot hold. On Postgres that is
# StringDataRightTruncation -> SQLAlchemy **DataError**, which is NOT the
# IntegrityError ``register_public`` catches -- so the request 500s having written
# neither an account nor an audit row.
#
# This suite runs on in-memory SQLite (CLAUDE.md -> "Why the tests run on SQLite"),
# and SQLite IGNORES declared VARCHAR lengths: inserting a 64-character badge there
# succeeds silently, which is exactly why the bug survived. So the truncation is
# asserted on the RETURNED STRING's length, never by inserting and expecting the DB
# to object -- an insert-based test would pass identically before and after the fix.


def test_the_cap_tracks_the_declared_column_width():
    """One number, read from the model rather than written down twice.

    If ``users.employee_id`` is ever widened (or narrowed) the constant has to move
    with it. Hard-coding 50 here would let the two desync silently in the direction
    that matters: a column narrowed below the cap re-opens the Postgres-only 500, and
    a column widened above it leaves the derivation truncating for no reason.
    """
    from app.models.user import User

    assert MAX_EMPLOYEE_ID_LENGTH == User.__table__.c.employee_id.type.length


def test_a_maximum_length_local_part_is_truncated_to_the_column_width():
    """The 500 itself: 64 characters in, 50 characters out.

    64 is not an arbitrary long string -- it is the RFC 5321 maximum for a local
    part, i.e. the longest value a legitimate registrant can put in front of the
    ``@``. Nothing about it is malformed; the address validates, the account is
    wanted, and before the cap it was the request that could not be served.
    """
    local_part = "a" * 64
    badge = employee_id_from_email(f"{local_part}@wercomfg.com", _never)

    assert badge == "a" * MAX_EMPLOYEE_ID_LENGTH
    assert len(badge) == MAX_EMPLOYEE_ID_LENGTH


def test_the_cap_covers_the_suffix_and_still_holds_once_it_reaches_two_letters():
    """The half a "just truncate the base once" fix gets wrong.

    Trimming the base before the loop and then appending ``-b`` produces 52
    characters -- the same DataError, now only on the collision path, which is the
    rarer and harder-to-reproduce half. The base is therefore re-trimmed INSIDE the
    loop against the suffix actually being applied, so the string shortens again when
    the suffix grows from one letter to two.

    Walked past ``-z`` deliberately: ``-aa`` is the first suffix that is three
    characters, so a fix that reserved a fixed two-character tail would pass every
    assertion up to ``-z`` and overrun here. (This test pinned ``-10`` before the walk
    suffixed with letters; the ROLL-OVER is the same edge, one alphabet along.)
    """
    base = "a" * MAX_EMPLOYEE_ID_LENGTH
    single_letter = [f"{'a' * (MAX_EMPLOYEE_ID_LENGTH - 2)}-{letter}" for letter in "bcdefghijklmnopqrstuvwxyz"]
    is_taken, probed = _taken(base, *single_letter)

    result = employee_id_from_email(f"{'a' * 64}@wercomfg.com", is_taken)

    assert result == f"{'a' * (MAX_EMPLOYEE_ID_LENGTH - 3)}-aa"
    assert len(result) == MAX_EMPLOYEE_ID_LENGTH
    # Every candidate OFFERED to the predicate is insertable too -- a probe the column
    # could not hold would 500 on the very first collision-free install.
    assert probed == [base, *single_letter, result]
    assert [len(candidate) for candidate in probed] == [MAX_EMPLOYEE_ID_LENGTH] * len(probed)
    assert max(len(candidate) for candidate in probed) <= MAX_EMPLOYEE_ID_LENGTH


def test_a_short_local_part_is_untouched_by_the_cap():
    """The regression half: the cap is a CEILING, not a reshaping.

    Every value here is one an existing test above already pins, restated against the
    capped implementation because a slicing bug is far more likely to be off by one on
    ordinary input than to fail on the 64-character edge -- ``base[:50]`` and
    ``base[:-50]`` differ on nothing shorter than the cap. The suffix walk is included
    because that is the branch the fix actually rewrote.
    """
    assert employee_id_from_email("jmw@wercomfg.com", _never) == "jmw"
    assert employee_id_from_email("JMW@wercomfg.com", _never) == "JMW"
    assert employee_id_from_email("john.doe+tag@wercomfg.com", _never) == "johndoetag"
    assert employee_id_from_email("+@example.com", _never) == "user"

    is_taken, probed = _taken("jmw", "jmw-b")
    assert employee_id_from_email("jmw@wercomfg.com", is_taken) == "jmw-c"
    assert probed == ["jmw", "jmw-b", "jmw-c"], "a short badge must not be trimmed to make room for its suffix"


# ===========================================================================
# 4. is_synthetic_email -- the READ side of the mint
# ===========================================================================
#
# The dispatcher (``services/notification_dispatch``) asks this before every email leg and
# records ``sent=False`` when the answer is True, so BOTH directions of a drift are
# damaging and neither is loud:
#
#   too NARROW  -> notification_logs regains rows claiming sent=True for an address with
#                  no mailbox behind it, which is the false-evidence defect the guard was
#                  added to remove;
#   too WIDE    -> a real person at a real domain silently stops being emailed, and the
#                  only trace is a log row saying their address is a placeholder.
#
# So the tests below are matched pairs: for every address that must be True there is a
# near-miss that must be False.


@pytest.mark.parametrize(
    "email",
    [
        "emp-1234@users.werco.com",  # minted right here for a badge-only account
        "EMP-1234@USERS.WERCO.COM",  # case-insensitive: the column is not normalized
        "Emp-Emp-0777@Users.Werco.Com",
        "legacy.import@werco.local",  # the LEGACY reserved domain, pre-repair
        "LEGACY.IMPORT@WERCO.LOCAL",
        "  emp-1234@users.werco.com  ",  # surrounding whitespace is not a real address change
    ],
)
def test_both_placeholder_domains_are_undeliverable_case_insensitively(email: str):
    assert is_synthetic_email(email) is True


def test_the_domains_it_matches_are_the_two_named_constants():
    """Pinned as constants, not literals, so the reader and the minter move together.

    ``synthetic_email_for_employee_id`` builds addresses at
    :data:`SYNTHETIC_EMAIL_DOMAIN`; if this predicate ever stopped covering that constant
    the dispatcher would start recording deliveries to every badge-only account again.
    """
    assert SYNTHETIC_EMAIL_DOMAIN == "users.werco.com"
    assert LEGACY_RESERVED_EMAIL_DOMAIN == "werco.local"
    assert is_synthetic_email(f"anything@{SYNTHETIC_EMAIL_DOMAIN}") is True
    assert is_synthetic_email(f"anything@{LEGACY_RESERVED_EMAIL_DOMAIN}") is True


def test_every_address_the_minter_produces_reads_back_as_synthetic():
    """The round trip that keeps the two halves of one contract together.

    Asserted over the same badge shapes §1 mints from -- including the punctuation
    fallback -- rather than one hand-written literal, because the failure mode is a mint
    whose output the reader does not recognise, and that shows up on the ODD shapes first.
    """
    for badge in ("EMP-0777", "0042", "keep_me.now-1", "Badge #42!", "###", ""):
        minted = synthetic_email_for_employee_id(badge, _never)
        assert is_synthetic_email(minted) is True, minted


@pytest.mark.parametrize(
    "email",
    [
        "jmw@wercomfg.com",  # the real company domain -- deliverable
        "someone@example.com",
        "operator@werco.com",  # NOT users.werco.com
    ],
)
def test_an_ordinary_address_is_deliverable(email: str):
    assert is_synthetic_email(email) is False


@pytest.mark.parametrize(
    "email",
    [
        # THE SUBSTRING TRAP. A real domain that merely CONTAINS a placeholder domain.
        # ``"users.werco.com" in email`` -- the obvious implementation -- answers True here
        # and silently stops mailing a real mailbox at somebody else's domain, which an
        # attacker can also arrange deliberately by registering the suffix.
        "bob@users.werco.com.example.org",
        "bob@werco.local.example.org",
        "bob@notusers.werco.com",  # ends with the domain, but is a different domain
        "bob@werco.localhost",  # ``.localhost`` is not ``.local``
        # THE LOCAL-PART TRAP, facing the other way: the domain text appears BEFORE the
        # final "@". Splitting on the FIRST "@" instead of the last, or searching the whole
        # string, answers True for an ordinary mailbox at example.org.
        "users.werco.com@example.org",
        "werco.local@example.org",
        "emp-1234@users.werco.com@example.org",
    ],
)
def test_a_real_domain_that_merely_contains_a_placeholder_domain_is_deliverable(email: str):
    assert is_synthetic_email(email) is False


@pytest.mark.parametrize("email", [None, "", "   ", "no-at-sign", "users.werco.com"])
def test_no_address_at_all_is_not_a_synthetic_address(email):
    """Absence is a different condition from "we minted it", and the caller judges it
    separately: ``notification_dispatch._undeliverable_email_reason`` tests emptiness
    FIRST and records its own distinct cause, precisely because "this account has no
    address" and "this address is a placeholder we minted" are different problems for
    whoever reads the delivery log. (That check used to be a bare ``and user.email``
    truthiness gate further down the leg, which dropped the empty case with no row at
    all.) Answering True for None here would work by accident today and mislead the next
    reader; answering None-ish would break the ``is True``/``is False`` contract."""
    assert is_synthetic_email(email) is False


# ===========================================================================
# 5. The ENUMERABILITY contract /auth/login's throttle keys on
# ===========================================================================
#
# ``is_synthetic_email`` has a second consumer besides the notification sender, and it is
# a security control rather than a delivery one: ``POST /auth/login`` decides whether to
# count a failed attempt against the per-IP throttle with
# ``(not is_email_login) or is_synthetic_email(submitted)``.
#
# The reason it can be keyed on this module at all is the property below -- a minted
# address is a PURE FUNCTION of the badge, so the address space is not merely "guessable",
# it is the ~10^4 badge space rewritten. Sweeping ``emp-0000@users.werco.com`` through
# ``emp-9999@…`` reaches the same accounts as sweeping badges 0000-9999 and drives the same
# 5-failure account lockout. The throttle's earlier rule -- exempt anything containing an
# ``"@"`` -- therefore exempted the exact population it was written to protect, since a
# badge-only account has no other address to be reached at.
#
# These tests pin the property, not the endpoint (``tests/api/test_auth_badge_password_
# login.py`` §5 drives the HTTP behavior). They are here because the property is this
# module's, and because a change here -- a random suffix, a hash, a per-tenant domain --
# would silently make the login rule stop describing reality.


@pytest.mark.parametrize("badge", ["0000", "0001", "4242", "9999", "EMP-0339", "emp-0339"])
def test_a_minted_address_is_a_pure_function_of_the_badge(badge: str):
    """Same badge in, same address out, with no lookup and no randomness.

    This is what makes the address space enumerable: an attacker who guesses a badge can
    COMPUTE the address, offline, and never needs to observe a response to learn the
    spelling. A future "improvement" that salted the local part would break this test --
    and should, because it would also invalidate the login throttle's rule.
    """
    assert synthetic_email_for_employee_id(badge, _never) == synthetic_email_for_employee_id(badge, _never)
    assert synthetic_email_for_employee_id(badge, _never) == f"emp-{badge.lower()}@{SYNTHETIC_EMAIL_DOMAIN}"


def test_every_address_a_badge_sweep_would_produce_reads_back_as_enumerable():
    """The whole four-digit keyspace, spot-swept: every minted address is recognised.

    ``is_synthetic_email`` is the predicate ``/auth/login`` throttles on, so a badge whose
    minted address answered False would be a hole in the sweep protection at exactly one
    point in the keyspace -- invisible to any test that checked one address.
    """
    for n in range(0, 10000, 137):  # 73 samples spread across the keyspace, cheap and even
        assert is_synthetic_email(synthetic_email_for_employee_id(f"{n:04d}", _never)) is True

    # ...and the legacy spelling of the same account, which older imports still hold.
    for n in range(0, 10000, 1371):
        assert is_synthetic_email(f"emp-{n:04d}@{LEGACY_RESERVED_EMAIL_DOMAIN}") is True


def test_an_ordinary_work_address_is_not_in_that_keyspace():
    """The boundary the throttle's exemption rests on, stated from this side.

    An address at a domain this system does not mint is not derivable from a badge, so
    counting failures against it would buy nothing and would let one mistyped password on a
    shared office NAT take a whole floor's login offline. The exemption is only safe while
    this stays False.
    """
    for address in ("opal.rivera@wercomfg.com", "jmw@example.com", "someone@users.werco.com.example.org"):
        assert is_synthetic_email(address) is False


# ===========================================================================
# 6. Both walks are BOUNDED -- an unusable predicate must RAISE, never loop
# ===========================================================================
#
# ``is_taken`` belongs to the caller, so this module cannot assume it is a function of the
# candidate -- and public registration's provably is not. ``auth._employee_id_taken``
# answers True when the collision probe's candidate window TRUNCATED, which describes the
# user TABLE rather than the candidate, so it keeps answering True for every suffix alike.
# An unbounded ``while is_taken(candidate)`` there never terminates, and each turn issues a
# database query from an UNAUTHENTICATED route: one request pins a worker forever and takes
# the connection with it.
#
# So each walk stops after :data:`MAX_IDENTIFIER_CANDIDATES` offers and raises
# ``IdentifierDerivationExhausted``. Returning the last candidate instead would hand the
# caller a value the predicate had just refused -- straight onto ``uq_users_company_email``
# / ``uq_users_company_employee_id`` as a 500, or worse, onto a real operator's badge.
#
# EVERY TEST BELOW GIVES THE WALK NO CHANCE TO HANG. ``_always_taken`` raises its own
# AssertionError once the offers pass the cap by a margin, so a build with the cap removed
# fails in milliseconds with a message naming the cause, rather than wedging an xdist
# worker until the whole run is killed and the failure reads as infrastructure.


def _always_taken(patience: int = MAX_IDENTIFIER_CANDIDATES + 50):
    """A predicate pinned True -- the shape a truncated collision window produces.

    ``patience`` is the anti-hang guard, not part of the contract: crossing it means the
    walk did NOT stop where it should have, and raising there converts an infinite loop
    into an ordinary, fast, readable test failure.
    """
    probed: list = []

    def is_taken(candidate: str) -> bool:
        probed.append(candidate)
        if len(probed) > patience:
            raise AssertionError(
                f"the walk never terminated: {len(probed)} candidates offered, cap is {MAX_IDENTIFIER_CANDIDATES}"
            )
        return True

    return is_taken, probed


def test_the_mint_raises_instead_of_looping_when_no_candidate_is_ever_free():
    """email <- badge, with nothing ever free: bounded, and bounded at the stated number."""
    is_taken, probed = _always_taken()

    with pytest.raises(IdentifierDerivationExhausted) as excinfo:
        synthetic_email_for_employee_id("0339", is_taken)

    assert len(probed) == MAX_IDENTIFIER_CANDIDATES, "the cap must bound the PROBE CALLS, i.e. the queries"
    assert len(set(probed)) == len(probed), "the walk offered the same candidate twice"
    assert probed[0] == f"emp-0339@{SYNTHETIC_EMAIL_DOMAIN}"
    assert probed[1] == f"emp-0339-2@{SYNTHETIC_EMAIL_DOMAIN}"
    assert probed[-1] == f"emp-0339-{MAX_IDENTIFIER_CANDIDATES}@{SYNTHETIC_EMAIL_DOMAIN}"

    message = str(excinfo.value)
    assert "0339" not in message, "the message can reach an exception log; it must carry no submitted value"
    assert str(MAX_IDENTIFIER_CANDIDATES) in message


def test_the_badge_derivation_raises_instead_of_looping_when_no_candidate_is_ever_free():
    """badge <- email, same contract. This is the walk public registration can pin True."""
    is_taken, probed = _always_taken()

    with pytest.raises(IdentifierDerivationExhausted) as excinfo:
        employee_id_from_email("jmw@wercomfg.com", is_taken)

    assert len(probed) == MAX_IDENTIFIER_CANDIDATES
    assert len(set(probed)) == len(probed)
    assert probed[0] == "jmw"
    assert probed[1] == "jmw-b"
    # The bare base occupies ordinal 1, so the Nth candidate offered is the Nth letter
    # string: offer 100 is ``cv`` (bijective base-26). Written as the literal it must be,
    # not recomputed with the implementation's own helper.
    assert probed[-1] == "jmw-cv"

    message = str(excinfo.value)
    assert "jmw" not in message, "the message can reach an exception log; it must carry no submitted value"
    assert str(MAX_IDENTIFIER_CANDIDATES) in message


@pytest.mark.parametrize(
    "walk, argument, expected",
    [
        (synthetic_email_for_employee_id, "0339", f"emp-0339-{MAX_IDENTIFIER_CANDIDATES}@{SYNTHETIC_EMAIL_DOMAIN}"),
        # The hundredth candidate the BADGE walk offers -- letters, so ``cv`` rather than
        # ``100``. The two walks reaching different last candidates is the asymmetry §7 pins.
        (employee_id_from_email, "jmw@wercomfg.com", "jmw-cv"),
    ],
)
def test_a_walk_that_converges_on_its_very_last_candidate_is_not_refused(walk, argument, expected):
    """The cap is an OFF-BY-ONE away from refusing signups that should succeed.

    The last candidate the cap permits must still be usable: raising one offer early would
    turn "the hundredth spelling was free" into a refused registration, and nothing in the
    response would say so (the route answers the uniform pending body either way). Asserted
    for both directions, because the two walks compose their candidates differently.
    """
    offers = {"n": 0}

    def is_taken(candidate: str) -> bool:
        offers["n"] += 1
        assert offers["n"] <= MAX_IDENTIFIER_CANDIDATES, "the walk went past the cap instead of stopping"
        return offers["n"] < MAX_IDENTIFIER_CANDIDATES

    assert walk(argument, is_taken) == expected
    assert offers["n"] == MAX_IDENTIFIER_CANDIDATES


def test_the_cap_is_sized_above_the_collision_counts_the_rest_of_this_file_exercises():
    """Sizing, from the direction that would bite an honest install.

    A cap of 5 is "bounded" too, and it would start refusing real signups: several imported
    badges can sanitize to one local part, and this file's own suffix tests already walk
    into double digits. The number has to clear ordinary collisions by a wide margin and
    still bound the pathological case -- so it is pinned here rather than left as a literal
    somebody trims while "tidying".
    """
    assert MAX_IDENTIFIER_CANDIDATES >= 20, "too low: ordinary collision clusters would be refused as pathological"

    # ...and it really is reachable as an ordinary walk: 19 taken candidates still converge.
    taken = [f"emp-0339-{n}@{SYNTHETIC_EMAIL_DOMAIN}" for n in range(2, 21)]
    is_taken, probed = _taken(f"emp-0339@{SYNTHETIC_EMAIL_DOMAIN}", *taken)
    assert synthetic_email_for_employee_id("0339", is_taken) == f"emp-0339-21@{SYNTHETIC_EMAIL_DOMAIN}"
    assert len(probed) == 21, "20 taken candidates, then the free one -- all inside the cap"


# ===========================================================================
# 7. The badge walk suffixes with LETTERS -- the property that makes it converge
# ===========================================================================
#
# THE BUG THIS SECTION IS THE REGRESSION TEST FOR. ``auth._normalize_employee_id`` reads
# any badge as its LAST FOUR DIGITS, zero-padded, so a numeric suffix does not step PAST a
# badge -- it steps INTO one. ``jmw-2`` IS badge ``0002``; ``jmw-3`` IS ``0003``. Public
# registration's collision probe refuses NORMALIZED collisions as well as exact ones (it
# has to: an inserted row that normalizes onto a real operator's badge makes every scan of
# that badge ambiguous and 409s the operator off the kiosk, the crew station and both login
# routes). So on a shop whose badges run contiguously from 0001 -- the ordinary shape --
# every candidate the numeric walk could offer collided with a real operator, the walk ran
# to its cap, and the registration was refused. Silently: the route answers the same
# uniform "submitted for approval" body whether it inserted a row or not, so nothing about
# the response distinguished a dropped signup from a successful one.
#
# Letters close it STRUCTURALLY rather than by widening the cap: a candidate carrying no
# digits normalizes to ``None``, i.e. it is not in the four-digit keyspace at all and
# cannot collide there for any table. What is left is exact-string collisions, which are
# finite. That is why the invariant below is asserted over the WHOLE sequence -- one
# sampled candidate would pass just as well under a walk that emitted digits every tenth
# step, and the failure only shows up on a customer's badge table.


def _full_walk(email: str) -> list:
    """Every candidate the badge walk offers for ``email``, in order, with nothing free.

    Uses the same anti-hang predicate §6 does, so a build with the cap removed fails fast
    with a message rather than wedging an xdist worker.
    """
    is_taken, probed = _always_taken()
    with pytest.raises(IdentifierDerivationExhausted):
        employee_id_from_email(email, is_taken)
    return probed


def test_the_badge_suffix_sequence_is_the_bijective_letter_alphabet():
    """The exact spellings, including the roll-over past ``-z``.

    Bijective base-26 (spreadsheet columns), not plain base-26: every ordinal maps to
    exactly one spelling and no spelling repeats. A plain mapping would emit ``a`` for both
    0 and 26, handing the walk a duplicate candidate -- which reads as "still taken" and
    burns an offer for nothing. ``-a`` itself is never offered because the BARE BASE
    already occupies that slot.
    """
    candidates = _full_walk("jmw@wercomfg.com")

    assert candidates[0] == "jmw", "the bare base is offered first, unsuffixed"
    assert candidates[1:5] == ["jmw-b", "jmw-c", "jmw-d", "jmw-e"]
    assert "jmw-a" not in candidates
    assert candidates[25] == "jmw-z", "the last single letter"
    assert candidates[26:29] == ["jmw-aa", "jmw-ab", "jmw-ac"], "the roll-over, which must not stop at 25"
    assert candidates[-1] == "jmw-cv", "the hundredth offer, i.e. where the cap stops the walk"
    assert len(candidates) == MAX_IDENTIFIER_CANDIDATES


def test_the_sequence_is_deterministic_and_never_repeats_a_candidate():
    """Two properties one assertion apart, and both are about wasted offers.

    A repeat is not merely untidy: the cap counts OFFERS, and a duplicate candidate is
    guaranteed to come back taken (the predicate just answered on it), so every repeat
    shortens the walk's real reach by one for nothing. Determinism matters because the
    caller's predicate hits the database -- a walk whose order varied would probe different
    rows on two identical requests and could converge on different badges for the same
    address.
    """
    first = _full_walk("jmw@wercomfg.com")
    second = _full_walk("jmw@wercomfg.com")

    assert first == second, "the same address must offer the same candidates in the same order"
    assert len(set(first)) == len(first), "the walk offered the same candidate twice"


def test_a_truncated_candidate_that_reconstructs_a_refused_spelling_is_skipped_not_re_offered():
    """The branch an ordinary walk never reaches, and what it costs when it is got wrong.

    The base is re-trimmed against the suffix ACTUALLY being applied (§3), so on a base
    already exactly ``MAX_EMPLOYEE_ID_LENGTH`` long, trimming it to make room for the first
    two-character tail ``-b`` can spell the base back out verbatim. A local part of
    ``'a' * 48 + '-b'`` is precisely that shape: it IS its own first suffixed candidate.

    Re-offering it is not merely untidy. The predicate has already refused that spelling, so
    it can only answer "taken" again -- and the cap counts OFFERS, so the walk would spend
    one of its hundred database queries to learn something it already knew, and reach one
    spelling less far. The walk therefore steps the ordinal WITHOUT spending an offer, which
    is what the second assertion pins: candidate two is the ``-c`` spelling, not a second
    ``-b``.
    """
    local_part = "a" * (MAX_EMPLOYEE_ID_LENGTH - 2) + "-b"
    assert len(local_part) == MAX_EMPLOYEE_ID_LENGTH, "precondition: the base is already at the column width"

    candidates = _full_walk(f"{local_part}@wercomfg.com")

    assert candidates[0] == local_part, "the bare base is offered first, unsuffixed"
    assert candidates[1] == "a" * (MAX_EMPLOYEE_ID_LENGTH - 2) + "-c", (
        "the ``-b`` candidate trims back to the base just refused, so it must be SKIPPED -- "
        "re-offering it burns one of the hundred queries on an answer already given"
    )
    assert candidates.count(local_part) == 1, "the reconstructed spelling was offered a second time"
    assert len(set(candidates)) == len(candidates), "the walk offered the same candidate twice"
    assert len(candidates) == MAX_IDENTIFIER_CANDIDATES, "the skip must cost the walk no offer, i.e. no reach"
    assert max(len(candidate) for candidate in candidates) <= MAX_EMPLOYEE_ID_LENGTH

    # The skipped ordinal shifts the whole tail along by one, so the hundredth OFFER is the
    # hundred-and-first letter string. Written out because it is the same off-by-one, one
    # alphabet along, that §8 guards the prose against.
    assert candidates[-1] == "a" * (MAX_EMPLOYEE_ID_LENGTH - 3) + "-cw"


def test_every_candidate_offered_for_a_digit_free_base_is_outside_the_badge_keyspace():
    """THE INVARIANT THE WHOLE FIX RESTS ON, asserted over the entire sequence.

    ``_normalize_employee_id`` returning ``None`` is what makes a candidate unable to
    collide with any badge under normalization -- and it is also what makes public
    registration's ``_employee_id_taken_reason`` answer "free" WITHOUT running the
    normalized probe, so a truncated candidate window cannot pin the predicate True either.
    Both traps close on this one property, so it is checked for all 100 candidates rather
    than for a sample: a walk that emitted a digit on, say, every 26th step would pass a
    spot check and strand exactly the registrations this fix exists to serve.
    """
    candidates = _full_walk("jmw@wercomfg.com")

    assert len(candidates) == MAX_IDENTIFIER_CANDIDATES
    for candidate in candidates:
        assert _normalize_employee_id(candidate) is None, f"{candidate} lands in the four-digit badge keyspace"

    # The counter-example, spelled out rather than described: the suffixes this replaced
    # were badge numbers, which is why a contiguous badge table defeated the old walk.
    assert _normalize_employee_id("jmw-2") == "0002"
    assert _normalize_employee_id("jmw-3") == "0003"
    assert _normalize_employee_id(f"jmw-{MAX_IDENTIFIER_CANDIDATES}") == "0100"


def test_a_digit_bearing_base_drops_to_a_digit_free_one_instead_of_suffixing_forever():
    """The one case letters cannot fix by themselves.

    ``jw2024`` normalizes to ``2024`` -- so if it collides, appending letters leaves that
    digit core in place and every candidate alike stays in the same normalized slot. The
    walk therefore drops to the sanitized local part with its digits STRIPPED and walks
    that. It happens only when the digit-bearing badge would have collided with a real
    operator's badge, so the alternative is refusing a legitimate signup or minting a badge
    that takes that operator off the floor: the derived badge is what gives way.
    """
    is_taken, probed = _taken("jw2024")

    assert employee_id_from_email("jw2024@wercomfg.com", is_taken) == "jw"
    assert probed == ["jw2024", "jw"], "the fallback base is offered directly, not suffixed first"
    assert _normalize_employee_id("jw2024") == "2024", "precondition: the base really was in the keyspace"
    assert _normalize_employee_id("jw") is None


def test_a_digit_bearing_base_that_strips_to_nothing_falls_back_to_user():
    """``0339@attacker.example.com`` -- the address-shaped version of a badge collision.

    Stripping the digits from ``0339`` leaves nothing, so the ``user`` fallback is what
    keeps the NOT NULL column filled. The registrant still gets an account; what they do
    not get is a badge in the operator's slot.
    """
    is_taken, probed = _taken("0339")

    assert employee_id_from_email("0339@attacker.example.com", is_taken) == "user"
    assert probed == ["0339", "user"]
    assert _normalize_employee_id("user") is None


def test_the_digit_free_fallback_then_walks_with_letters_too():
    """The fallback base is a base like any other -- it takes the same letter walk."""
    is_taken, probed = _taken("jw2024", "jw", "jw-b")

    assert employee_id_from_email("jw2024@wercomfg.com", is_taken) == "jw-c"
    assert probed == ["jw2024", "jw", "jw-b", "jw-c"]

    candidates = _full_walk("jw2024@wercomfg.com")
    assert candidates[0] == "jw2024" and candidates[1] == "jw"
    for candidate in candidates[1:]:
        assert _normalize_employee_id(candidate) is None, f"{candidate} re-entered the badge keyspace"
    assert len(candidates) == MAX_IDENTIFIER_CANDIDATES


def test_every_candidate_fits_the_column_even_once_the_suffix_is_two_letters():
    """The cap and the column, checked together across the whole walk.

    §3 pins the returned value; this pins every candidate OFFERED, which is the half that
    reaches the database as a bind parameter and (on the accepting install) becomes an
    INSERT. A 51-character probe would be a Postgres-only DataError on the first collision.
    """
    candidates = _full_walk(f"{'a' * 64}@wercomfg.com")

    assert max(len(candidate) for candidate in candidates) <= MAX_EMPLOYEE_ID_LENGTH
    assert len(set(candidates)) == len(candidates)
    assert candidates[26].endswith("-aa"), "precondition: the walk really did reach a two-letter suffix"
    assert len(candidates[26]) == MAX_EMPLOYEE_ID_LENGTH, "the base is re-trimmed against the LONGER suffix"
    assert candidates[-1].endswith("-cv")
    assert len(candidates[-1]) == MAX_EMPLOYEE_ID_LENGTH


def test_the_two_walks_suffix_differently_and_that_asymmetry_is_the_correctness_property():
    """Stated in one place so nobody "unifies" the two sequences.

    The mint's collisions are whole EMAIL ADDRESSES: a digit inside an address means
    nothing to any resolver, so ``-2`` is inert there and its outputs are pinned by §1. The
    badge walk's collisions are BADGES, where a digit is a coordinate. Same-looking loops,
    opposite keyspaces -- and only one of them may carry digits.
    """
    mint_taken, _ = _taken(f"emp-0339@{SYNTHETIC_EMAIL_DOMAIN}")
    assert synthetic_email_for_employee_id("0339", mint_taken) == f"emp-0339-2@{SYNTHETIC_EMAIL_DOMAIN}"

    badge_taken, _ = _taken("jmw")
    assert employee_id_from_email("jmw@wercomfg.com", badge_taken) == "jmw-b"

    assert _normalize_employee_id("jmw-2") == "0002", "a numeric badge suffix IS a badge number"
    assert _normalize_employee_id("jmw-b") is None, "a letter suffix is not"


# ===========================================================================
# 8. The documented example and the walk cannot disagree
# ===========================================================================
#
# THE DEFECT THIS SECTION IS THE REGRESSION TEST FOR, and it is a documentation defect on
# purpose. The cap was illustrated by hand in two places -- ``IdentifierDerivationExhausted``'s
# docstring and ``docs/API.md`` -- as "100 real accounts holding ``jmw``, ``jmw-b`` ...
# ``jmw-cv``", and both said ``jmw-cw``: one letter string PAST where the walk actually
# stops. Nothing executes a docstring, which is exactly why it survived being written, read
# and reviewed. The only thing that can hold prose to code is a test that reads the prose.
#
# The guard is deliberately WIDER than the one wrong token. It asserts that EVERY
# ``jmw-<letters>`` spelling anywhere in the service source or the API reference is a
# candidate the walk really offers -- which catches ``jmw-a`` (never emitted; the bare base
# holds that slot), catches any future off-by-one alike, and does not need updating when a
# new example is added correctly. The numeric counter-examples the prose cites on purpose
# (``jmw-2``, ``jmw-3`` -- the spelling this walk REPLACED, §7) are not letter strings and
# are ignored by construction.
#
# §7 owns the literal ``jmw-cv``; these tests deliberately do not restate it, so the two
# halves fail for different reasons: §7 when the WALK changes, §8 when the walk and the
# prose stop agreeing.

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
API_DOC = REPO_ROOT / "docs" / "API.md"
USER_IDENTITY_SOURCE = pathlib.Path(user_identity_module.__file__)

# Letters only, so the numeric counter-examples are skipped rather than flagged.
_JMW_LETTER_EXAMPLE = re.compile(r"jmw-([a-z]+)\b")

# A regex that stopped matching would make every assertion below pass VACUOUSLY. Floored at
# what each source carried when this guard was written. Fix the parser if a floor trips --
# do NOT lower the floor.
_MIN_EXAMPLES_IN_SOURCE = 3
_MIN_EXAMPLES_IN_API_DOC = 8

# The exact wrong spelling that shipped, named so a regression fails with the token in the
# message rather than with "some string is missing".
_THE_OFF_BY_ONE = "jmw-cw"


def _documented_jmw_examples(text: str) -> list:
    return [f"jmw-{suffix}" for suffix in _JMW_LETTER_EXAMPLE.findall(text)]


def _prose_sources() -> dict:
    """The files whose ``jmw-`` examples are held to the walk, read as text.

    Asserted present rather than skipped: a guard that quietly does nothing when run from a
    partial checkout is worth less than no guard, because the suite still reports green.
    """
    for path in (USER_IDENTITY_SOURCE, API_DOC):
        assert path.is_file(), (
            f"Cannot find {path}. This guard reads the prose directly and must never be "
            f"skipped; run the backend suite from a full repo checkout."
        )
    return {
        "the service source": USER_IDENTITY_SOURCE.read_text(),
        "docs/API.md": API_DOC.read_text(),
    }


def test_every_documented_badge_example_is_a_candidate_the_walk_really_offers():
    """Prose held to code, over every example rather than the one that was wrong.

    An example spelling that the walk never emits is a reader being told, in the reference
    an integrator reads, that this system issues a badge it does not issue. ``jmw-a`` is the
    shape that would be written by anyone who assumed the suffixes start at the first
    letter, and the final assertion proves the guard can actually catch it.
    """
    offered = set(_full_walk("jmw@wercomfg.com"))
    sources = _prose_sources()

    found = {name: _documented_jmw_examples(text) for name, text in sources.items()}
    assert len(found["the service source"]) >= _MIN_EXAMPLES_IN_SOURCE, (
        f"the example parse looks broken: {len(found['the service source'])} found in "
        f"{USER_IDENTITY_SOURCE.name}. Fix the parser -- do NOT lower the floor."
    )
    assert len(found["docs/API.md"]) >= _MIN_EXAMPLES_IN_API_DOC, (
        f"the example parse looks broken: {len(found['docs/API.md'])} found in docs/API.md. "
        f"Fix the parser -- do NOT lower the floor."
    )

    for name, examples in found.items():
        for example in examples:
            assert example in offered, (
                f"{name} documents {example} as a badge this walk issues, but the walk never "
                f"offers it. Correct the prose, or the sequence -- they disagree."
            )

    assert "jmw-a" not in offered, "precondition: the assertion above really can catch a wrong spelling"


def test_the_cap_example_in_the_prose_is_the_LAST_candidate_the_walk_offers():
    """The specific claim both docstring and reference make: where the hundredth offer lands.

    Read from the walk, never written down here -- §7 is where the literal is pinned, and
    duplicating it would mean a deliberate change to the sequence had to be applied in three
    places instead of two, which is how prose falls behind in the first place.
    """
    candidates = _full_walk("jmw@wercomfg.com")
    last = candidates[-1]
    sources = _prose_sources()

    assert len(candidates) == MAX_IDENTIFIER_CANDIDATES, "precondition: the walk really ran to the cap"

    assert last in IdentifierDerivationExhausted.__doc__, (
        f"the exception's docstring illustrates the cap without naming {last}, the candidate "
        f"the walk actually stops on"
    )
    assert last in sources["docs/API.md"], f"docs/API.md illustrates the cap without naming {last}"

    for name, text in sources.items():
        assert (
            _THE_OFF_BY_ONE not in text
        ), f"{name} is back on {_THE_OFF_BY_ONE}, one letter string past where the walk stops"
    assert _THE_OFF_BY_ONE not in candidates, "precondition: that spelling really is never offered"
