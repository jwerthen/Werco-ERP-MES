"""The ONE shape for deriving a user's missing identifier from the one they have.

``User.email`` and ``User.employee_id`` are both ``nullable=False`` (models/user.py),
but the people this system signs in do not all have both. A shop-floor operator's real
credential is the badge; an office registrant's is the address. So two derivations exist,
and they run in OPPOSITE directions:

* ``synthetic_email_for_employee_id`` -- email <- badge, for badge-only users. Mints
  ``emp-<sanitized-badge>@users.werco.com`` so the NOT NULL column gets a real, parseable
  value instead of the column being widened. ``_find_user_by_auth_email`` already knows
  this domain (it maps a repaired address back onto a legacy ``@werco.local`` row), so
  the shape is load-bearing, not cosmetic.
* ``employee_id_from_email`` -- badge <- email, for email-only registrants.

``is_synthetic_email`` is the third function here and the READ side of the first: it answers
"is this address one we minted and cannot deliver to?" so a sender can refuse to record a
delivery that could only bounce. It lives beside the minter on purpose -- the set of
placeholder domains and the code that creates them must never drift apart.

Both derivations were already implemented, twice each in slightly different words, and a third copy
was about to be added for ``POST /auth/register-public``. They are extracted here so the
sanitizer regexes, the ``"employee"`` / ``"user"`` all-punctuation fallbacks, the ``emp-``
prefix and the suffix sequences exist once. Existing tests pin those exact outputs; change
them here and every seam changes together, which is the point.

**THE TWO WALKS SUFFIX DIFFERENTLY, AND THAT ASYMMETRY IS THE CORRECTNESS PROPERTY.**
Do not "unify" them:

* the mint steps ``-2``, ``-3``, ... because its collisions are whole EMAIL ADDRESSES.
  A digit inside an address means nothing to any resolver, so a numeric suffix is inert
  there -- and its outputs are pinned by existing tests.
* the badge walk steps ``-b``, ``-c``, ... ``-z``, ``-aa``, ... because a digit inside a
  BADGE is a coordinate. ``auth._normalize_employee_id`` collapses any badge to its last
  four digits, so ``jmw-2`` IS badge ``0002`` -- and since the registration probe now
  refuses NORMALIZED collisions too, a shop with contiguous badge numbers made every
  numeric suffix collide and the walk could not converge at all. It exhausted at the cap
  and dropped a legitimate email-only signup behind the uniform "submitted for approval"
  body, having created nothing. Letters carry no digits, so they cannot land in that
  keyspace; see :func:`employee_id_from_email` for the full argument and for the one case
  (a digit-bearing local part) that needs a second move.

**Dedup SCOPE is deliberately the caller's**, passed in as an ``is_taken`` predicate
rather than baked in, because the callers legitimately disagree about it:

* the CSV importer (``api/endpoints/users.py``) dedups against a preloaded, lowercased,
  **per-company** set -- it is an authenticated write into one tenant, and it must not
  issue a probe per candidate inside a row loop;
* the legacy login repair (``api/endpoints/auth.py``) probes the DB **install-wide**, and
  must treat the user's OWN row as free so a repair can converge on the address the row
  already holds;
* public registration probes the DB **install-wide** too, matching the duplicate checks
  around it -- a per-company probe there could mint an address that already exists in
  another tenant, which is exactly the ambiguity that makes email login refuse 409.

No function here touches the database or the session. They are pure given the predicate.

**Both walks are BOUNDED, and the bound is not a tidiness measure.** ``is_taken`` is the
caller's, so this module cannot assume it is a function of the candidate. Public
registration's predicate provably is not: ``auth._employee_id_taken`` answers True when the
collision probe's candidate window TRUNCATED, an outcome that does not change as the
candidate changes -- so an unbounded ``while is_taken(candidate)`` there never terminates,
and every iteration issues a database query from an unauthenticated route. The cap turns
that into :class:`IdentifierDerivationExhausted`, which a caller must handle; returning a
possibly-colliding value instead would push the failure onto the NOT NULL unique columns
(``uq_users_company_email`` / ``uq_users_company_employee_id``) as a 500, or worse, onto a
real operator's badge.

**On the badge walk the cap is now a BACKSTOP rather than a working limit**, and the
letter suffixes are what demoted it. Both traps above close at once, for the same reason:
a candidate with no digits normalizes to ``None``, and ``auth._employee_id_taken_reason``
returns "free" on that without ever running the normalized probe -- so a digit-free
candidate can neither collide in the 4-digit keyspace NOR be pinned True by a truncated
window, and the predicate becomes a function of the candidate again. What is left is
exact-match collisions, which are finite. **Every caller must still handle the
exception** -- all three seams that call these walks do (``auth.register_public``,
``auth._ensure_valid_auth_email``, ``users``' CSV importer) -- because the mint's walk is
unchanged, ``is_taken`` remains the caller's to define, and an uncaught exception here is
a 500 on sign-in or a mid-import abort rather than a refusal.

**The extraction is NOT complete.** ``services/company_onboarding.py`` still carries its
own inline badge-from-email copy, and that one has neither a dedup walk nor the length
cap below -- so an admin address with a 51+ character local part still overruns
``employee_id String(50)`` and 500s company creation, invisibly under SQLite. It was left
alone deliberately (it is a different route, on a different change), not overlooked. Route
it through here when that route is next touched, rather than adding a fifth copy.
"""

import re
from typing import Callable, Optional

# The reserved domain synthetic addresses are minted under. Not a deliverable mailbox --
# it exists so a badge-only user still satisfies the NOT NULL email column.
SYNTHETIC_EMAIL_DOMAIN = "users.werco.com"

# The LEGACY reserved domain older imports stored addresses under, still present on any
# row whose user has not logged in since (``auth._ensure_valid_auth_email`` rewrites it to
# SYNTHETIC_EMAIL_DOMAIN on the first successful login, and only then). RFC 6762 reserves
# ``.local`` for multicast DNS, so it is not a routable mail domain and never resolves to
# a mailbox.
LEGACY_RESERVED_EMAIL_DOMAIN = "werco.local"

# Domains this system mints for itself and never delivers to. Membership is exact-match on
# the domain, never a substring test -- an ordinary address at a real domain that merely
# ENDS with one of these words (``bob@users.werco.com.example.org``) is deliverable.
_UNDELIVERABLE_EMAIL_DOMAINS = frozenset({SYNTHETIC_EMAIL_DOMAIN, LEGACY_RESERVED_EMAIL_DOMAIN})

# ``users.employee_id`` is ``String(50)``. An email local part may legally be 64
# characters (RFC 5321), so a derived badge can overrun the column: Postgres raises
# StringDataRightTruncation -> SQLAlchemy DataError, which is NOT the IntegrityError
# the public-registration handler catches, so the request 500s with no account and no
# audit row. SQLite ignores VARCHAR lengths, so no test could see it. Capped here --
# the one place the derivation lives -- rather than at each caller.
MAX_EMPLOYEE_ID_LENGTH = 50

# How many candidates EITHER walk will offer ``is_taken`` before refusing to keep walking.
#
# Sized from both directions. It cannot be small: each walk steps once per genuinely
# colliding row, and a legitimate install can hold a handful of ``jmw``/``jmw-b``/... rows
# (``emp-0339``/``emp-0339-2``/... on the mint side) or several imported badges that
# sanitize to the same local part, so a cap of 5 would start refusing honest signups. It
# cannot be large either: on the DB-probing callers each step is a query, and the
# pathological case this exists for -- a predicate stuck on True -- pays the full cap every
# time. 100 is comfortably past any real collision count for one local part while keeping
# the worst case to roughly the ~100 ms of bcrypt the same request already spends, so
# exhaustion does not stand out as a timing signal on the registration path.
MAX_IDENTIFIER_CANDIDATES = 100


class IdentifierDerivationExhausted(Exception):
    """No free candidate within :data:`MAX_IDENTIFIER_CANDIDATES` offers.

    Its own type, and catchable, BECAUSE the alternatives are both wrong: looping forever
    hangs a request handler, and returning the last candidate anyway hands the caller a
    value ``is_taken`` just said not to use. A caller decides what that means for its
    route -- ``auth.register_public`` refuses the registration exactly as it refuses a
    taken identifier (uniform pending body, no insert, an audit row naming this cause).

    The message deliberately carries NO identifier: it can reach an exception log, and
    the values passing through here are submitted credentials-adjacent input.

    **On the badge walk this is now genuinely unreachable in ordinary operation.** That
    walk offers only digit-free candidates, which ``auth._normalize_employee_id`` maps to
    ``None``, so no candidate can collide in the 4-digit badge keyspace and convergence is
    bounded by exact-match collisions alone -- 100 accounts would have to hold ``jmw``,
    ``jmw-b`` ... ``jmw-cv`` for real. It stays catchable anyway: an ``is_taken`` predicate
    is the caller's, this module cannot prove what it depends on, and a walk that hangs a
    request handler is the outcome the type exists to make impossible. Handle it; do not
    delete the handler on the grounds that it cannot fire.
    """


def synthetic_email_for_employee_id(employee_id: str, is_taken: Callable[[str], bool]) -> str:
    """Mint ``emp-<sanitized-employee-id>@users.werco.com`` for a badge-only user.

    The badge is lowercased and stripped to the characters an email local part may
    safely carry. A badge that sanitizes to nothing (all punctuation) falls back to
    ``employee`` so the column always gets a real value.

    ``is_taken`` decides collisions in whatever scope the caller owns; on a hit the
    candidate gains a ``-2``, ``-3``, ... suffix before the ``@``.

    Raises :class:`IdentifierDerivationExhausted` after
    :data:`MAX_IDENTIFIER_CANDIDATES` candidates have been offered and rejected -- see
    the module docstring for why an unbounded walk is not merely slow.
    """
    local_part = re.sub(r"[^a-z0-9._-]", "", (employee_id or "").lower())
    if not local_part:
        local_part = "employee"

    base = f"emp-{local_part}"
    candidate = f"{base}@{SYNTHETIC_EMAIL_DOMAIN}"
    suffix = 2
    # Counts candidates OFFERED, so the cap bounds the predicate calls (and therefore the
    # queries a DB-probing caller issues), not the suffix value.
    offered = 1
    while is_taken(candidate):
        if offered >= MAX_IDENTIFIER_CANDIDATES:
            raise IdentifierDerivationExhausted(
                f"no free synthetic email within {MAX_IDENTIFIER_CANDIDATES} candidates"
            )
        candidate = f"{base}-{suffix}@{SYNTHETIC_EMAIL_DOMAIN}"
        suffix += 1
        offered += 1
    return candidate


def is_synthetic_email(email: Optional[str]) -> bool:
    """True when the address is a placeholder this system minted and cannot deliver to.

    Two domains qualify, and including the second is a DELIBERATE WIDENING that changes
    behavior for pre-existing rows -- say so rather than discover it later:

    * ``@users.werco.com`` (:data:`SYNTHETIC_EMAIL_DOMAIN`) -- minted right here by
      :func:`synthetic_email_for_employee_id` for a badge-only account. A real-looking
      domain with no mailbox behind it.
    * ``@werco.local`` (:data:`LEGACY_RESERVED_EMAIL_DOMAIN`) -- the legacy reserved domain
      from old imports. It is equally undeliverable (RFC 6762 special-use), and a row only
      stops carrying one after that user's FIRST SUCCESSFUL LOGIN, when
      ``auth._ensure_valid_auth_email`` repairs it. So legacy rows that have never logged
      in stop being logged as delivered too. That is the honest direction: nothing was ever
      delivered to them, and the previous silence was the defect.

    Matching is exact on the domain after the FINAL ``@`` and case-insensitive. Never a
    substring test: ``bob@users.werco.com.example.org`` is an ordinary address at a real
    domain and must stay deliverable. None/empty (and any value with no ``@``) is not a
    synthetic address -- it is no address at all, which callers judge separately.
    """
    value = (email or "").strip().lower()
    if "@" not in value:
        return False
    return value.rsplit("@", 1)[1] in _UNDELIVERABLE_EMAIL_DOMAINS


# The badge walk's suffix alphabet. Letters only, and that is load-bearing rather than
# stylistic -- ``auth._normalize_employee_id`` reads a badge as its last four DIGITS, so a
# digit in a suffix is a badge number. See :func:`employee_id_from_email`.
_BADGE_SUFFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Any decimal digit, anywhere. Used to answer "could this candidate land in the normalized
# badge keyspace at all?" -- the same question ``_normalize_employee_id`` answers with
# ``None`` when it finds no digits.
_ANY_DIGIT = re.compile(r"\d")

# Any letter or digit, anywhere. The sanitizer keeps ``-`` and ``_`` (they are legitimate
# INSIDE a badge), so stripping the digits out of a local part that was digits-and-
# separators leaves a badge of bare punctuation -- ``2024-05`` -> ``-``. That is not an
# empty string, so an ``or "user"`` fallback does not catch it, and ``-`` is a value a real
# person would then be handed on a badge card. "Has at least one alphanumeric" is the test
# that means what the fallback was always reaching for. Deliberately NOT ``\w``, which
# counts ``_`` as a word character and would keep ``_`` as a badge.
_ANY_ALNUM = re.compile(r"[a-zA-Z0-9]")


def _letter_suffix(ordinal: int) -> str:
    """The ``ordinal``-th letter suffix, bijective base-26: 1 -> ``a``, 26 -> ``z``, 27 -> ``aa``.

    Bijective (spreadsheet-column) rather than plain base-26 so the sequence is a
    BIJECTION onto non-empty letter strings: every ordinal >= 1 maps to exactly one
    spelling, no spelling repeats, and there is no empty or leading-``a``-padded value. A
    plain base-26 mapping would emit ``a`` for both 0 and 26 and hand the walk a duplicate
    candidate -- which reads as "still taken" and burns an offer for nothing.

    Unbounded on purpose: the sequence must not run out at 25, so ``-z`` is followed by
    ``-aa``, ``-ab``, ... The walk's own cap is what stops it, not the alphabet.
    """
    letters = ""
    while ordinal > 0:
        ordinal, remainder = divmod(ordinal - 1, 26)
        letters = _BADGE_SUFFIX_ALPHABET[remainder] + letters
    return letters


def employee_id_from_email(email: str, is_taken: Callable[[str], bool]) -> str:
    """Derive a badge from an address's local part, e.g. ``jmw@wercomfg.com`` -> ``jmw``.

    Case is PRESERVED (the badge column is matched case-insensitively but stored as
    given). A local part carrying no alphanumeric character falls back to ``user``
    (it does not necessarily sanitize to nothing -- ``-`` and ``_`` survive the sanitizer) so the NOT NULL column never gets an empty string.

    The result is capped at ``MAX_EMPLOYEE_ID_LENGTH`` -- including the suffix, which is
    why the base is re-trimmed against the suffix actually being applied rather than once
    up front. A long address therefore yields a truncated badge instead of a 500. That
    re-trimming is also why the walk remembers what it has offered: on a base already at
    the cap, trimming it to fit a suffix can spell the base back out, and a candidate is
    never offered twice.

    **THE SUFFIXES ARE LETTERS, AND THAT IS WHAT MAKES THIS WALK CONVERGE.** On a hit the
    candidate gains ``-b``, ``-c``, ... ``-z``, then ``-aa``, ``-ab``, ... (bijective
    base-26, so the space is never exhausted at 25). ``-a`` is deliberately never emitted:
    the BARE BASE occupies that slot, so the sequence starts at the second element.

    The property this rests on, stated because every future edit here depends on it:
    ``auth._normalize_employee_id`` reads a badge as its LAST FOUR DIGITS, so **a
    candidate carrying no digits normalizes to ``None`` and can NEVER collide in the
    4-digit badge keyspace.** Only exact-string matches remain, and those are finite --
    which is what bounds convergence. Numeric suffixes had the opposite property and could
    not converge at all: ``jmw-2`` *is* badge ``0002``, ``jmw-3`` *is* ``0003``, so on a
    shop with contiguous badge numbers (and a probe that refuses normalized collisions,
    which ``auth.register_public``'s now does) every candidate collided, the walk hit the
    cap, and a legitimate email-only registration was silently dropped behind a 200.

    **The digit-bearing base is the one case letters cannot fix by themselves.** If the
    sanitized local part carries digits of its own (``jw2024`` -> normalizes to ``2024``),
    appending letters does not remove that digit core, so a normalized collision would
    persist across every candidate alike. So when the base itself comes back taken, the
    walk drops to a provably digit-free base -- the sanitized local part with its digits
    stripped, or ``user`` if that leaves no ALPHANUMERIC character (``2024-05`` strips to
    ``-``, which is not empty and is not a badge anybody should be issued) -- and walks
    THAT with letters.

    ``is_taken`` decides collisions in whatever scope the caller owns.

    Raises :class:`IdentifierDerivationExhausted` after
    :data:`MAX_IDENTIFIER_CANDIDATES` candidates have been offered and rejected. It is a
    backstop now rather than a working limit (see its docstring), but it is still raised
    rather than returning a candidate the predicate just refused, and every caller handles
    it -- an uncaught one is a 500 on the login path or a mid-row abort in the importer.
    """
    # The sanitizer KEEPS ``-`` and ``_``, so an emptiness test is not enough: ``----@x.co``
    # survives it whole and would put a badge reading "----" on a real person's card. Same
    # alphanumeric test the digit-stripped fallback below uses -- the two paths must agree,
    # which is why one of them having only the emptiness check was a defect and not a style
    # difference. Truncation is applied BEFORE the test so the value actually emitted is the
    # value checked.
    sanitized = re.sub(r"[^a-zA-Z0-9\-_]", "", (email or "").split("@")[0])[:MAX_EMPLOYEE_ID_LENGTH]
    base = sanitized if _ANY_ALNUM.search(sanitized) else "user"

    # Every candidate OFFERED to the predicate (see the twin above), which bounds this walk
    # twice over. Its SIZE is the cap, so the cap still bounds the predicate calls -- and
    # therefore the queries a DB-probing caller issues -- rather than the suffix value. Its
    # MEMBERSHIP is what stops a spelling being offered a SECOND time, which TRUNCATION can
    # otherwise produce: a sanitized local part of ``'a' * 48 + '-b'`` is exactly
    # MAX_EMPLOYEE_ID_LENGTH long, so trimming it to make room for the first suffix ``-b``
    # reconstructs it verbatim. Re-offering a spelling the predicate has already refused
    # reads as "still taken" and burns an offer for nothing.
    #
    # Only the BASE spellings can be hit this way -- two suffixed candidates can never
    # collide with each other, because where the shorter tail puts its leading ``-`` the
    # longer tail still has a letter -- but the set is kept general rather than special-
    # casing the base, so a later edit to the suffix sequence cannot reopen the hole.
    offered: set[str] = set()

    def _exhausted() -> IdentifierDerivationExhausted:
        return IdentifierDerivationExhausted(f"no free employee ID within {MAX_IDENTIFIER_CANDIDATES} candidates")

    offered.add(base)
    if not is_taken(base):
        return base

    if _ANY_DIGIT.search(base):
        # The base holds digits, so it sits in the normalized badge keyspace and the
        # collision just reported may well BE a normalized one -- ``is_taken`` is a bool
        # and cannot say which. It does not need to: under the probe this feeds, an exact
        # collision on a digit-bearing badge implies a normalized one anyway (the exactly
        # colliding row normalizes to the same four digits as every letter-suffixed
        # variant of it), so continuing to suffix this base is a search that provably
        # cannot converge. Drop to a base with no digits at all instead.
        #
        # STRIPPING THE DIGITS IS DELIBERATELY PREFERRED TO DROPPING A LEGITIMATE SIGNUP.
        # It only happens when the digit-bearing badge would have collided with a real
        # operator's badge -- i.e. the alternative is either refusing the registration
        # (silently, behind the uniform pending body) or minting a badge that takes that
        # operator off the kiosk and both login routes with a 409. A derived badge is a
        # placeholder for a user who never had one; an operator's badge is their
        # credential. The derived one is what gives way.
        #
        # The fallback tests for an ALPHANUMERIC, not for emptiness: the sanitizer keeps
        # ``-`` and ``_``, so a local part of digits and separators (``2024-05``) strips to
        # ``-``, which is non-empty and would be handed to a person as their badge.
        # Truncate FIRST and test the value actually being emitted -- a long enough local
        # part can strip to something whose first MAX_EMPLOYEE_ID_LENGTH characters are all
        # separators even though the untruncated string carries letters further along.
        stripped = _ANY_DIGIT.sub("", sanitized)[:MAX_EMPLOYEE_ID_LENGTH]
        base = stripped if _ANY_ALNUM.search(stripped) else "user"
        if len(offered) >= MAX_IDENTIFIER_CANDIDATES:  # pragma: no cover - only reachable if the cap is lowered to 1
            # Every offer is gated by the cap, this one included, so the invariant holds
            # structurally rather than by argument. Unreachable at today's cap of 100
            # (exactly one offer has been made at this point), which is why it is excluded
            # from coverage rather than tested.
            raise _exhausted()
        # Cannot already be in ``offered``: the branch is entered only when the base
        # carries a digit, and this one provably carries none.
        offered.add(base)
        if not is_taken(base):
            return base

    # Ordinal 1 is ``-a``; the bare base already occupied that slot, so start at 2 -> ``-b``.
    ordinal = 2
    while True:
        tail = f"-{_letter_suffix(ordinal)}"
        candidate = f"{base[: MAX_EMPLOYEE_ID_LENGTH - len(tail)]}{tail}"
        ordinal += 1
        if candidate in offered:
            # Truncation reconstructed a spelling already refused above. Step the ordinal
            # WITHOUT spending an offer: the predicate would only repeat its own answer,
            # and the cap exists to bound predicate calls, not loop turns. This cannot spin
            # -- it skips only spellings already offered, and that set is capped.
            continue
        if len(offered) >= MAX_IDENTIFIER_CANDIDATES:
            raise _exhausted()
        offered.add(candidate)
        if not is_taken(candidate):
            return candidate
