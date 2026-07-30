"""Guard for the **PYSEC-2026-1325 (`ecdsa`) advisory suppression** documented in
``docs/SECURITY_ADVISORY_SUPPRESSIONS.md`` (recorded there as tolerated-with-
rationale rather than a scanner ``--ignore`` — either way, this module is the
executable half of that rationale).

``ecdsa`` 0.19.2 carries PYSEC-2026-1325 — the "Minerva" P-256 timing
side-channel, the same defect as CVE-2024-23342 — with **no fixed version**
(affected spec ``>=0``). It is in the tree only transitively, via
``python-jose[cryptography]==3.5.0``. The suppression rests entirely on the
vulnerable code path being **structurally unreachable**, and these tests pin that
structure so it cannot quietly stop being true:

1. The vulnerable API is ``ecdsa.SigningKey.sign_digest()``, reachable only
   through python-jose's ``ecdsa_backend``.
2. ``jose/backends/__init__.py`` imports
   ``jose.backends.ecdsa_backend.ECDSAECKey`` **only** inside an
   ``except ImportError:`` fallback for the *cryptography* backend. Because
   ``cryptography`` is pinned directly in ``requirements.txt``, that fallback can
   never fire — so ``jose.backends.ECKey`` resolves to
   ``CryptographyECKey`` and the ``ecdsa`` module is never even imported.
3. This app signs and verifies JWTs with **HS256 (HMAC) exclusively**
   (``settings.ALGORITHM``), so the EC key classes are never constructed at all.
   ``app/core/security.py`` is the single ``from jose import ...`` site in
   ``app/`` and every encode/decode there passes ``settings.ALGORITHM``.

If someone drops the ``[cryptography]`` extra, unpins ``cryptography``, or
switches the app to an EC algorithm (ES256/384/512), these tests fail and the
suppression has to be re-reviewed rather than silently inherited.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import settings

pytestmark = pytest.mark.unit

# backend/ — the import root for the subprocess check below.
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_jose_ec_backend_is_cryptography_not_ecdsa():
    """``jose.backends.ECKey`` must be the *cryptography* implementation.

    Order-independent (an identity check on an already-imported name), so it
    holds under ``-n auto`` regardless of what else the suite has imported.
    """
    from jose.backends import ECKey
    from jose.backends.cryptography_backend import CryptographyECKey

    assert ECKey is CryptographyECKey, (
        f"jose.backends.ECKey resolved to {ECKey.__module__}.{ECKey.__name__}; the cryptography backend "
        "lost, which means the vulnerable ecdsa backend is now in play — re-review the PYSEC-2026-1325 "
        "suppression in docs/SECURITY_ADVISORY_SUPPRESSIONS.md"
    )


def test_jwt_algorithm_is_hmac():
    """HS256 keeps every EC code path unreachable regardless of backend."""
    assert settings.ALGORITHM == "HS256", (
        f"ALGORITHM is {settings.ALGORITHM!r}, not HS256. An EC algorithm (ES*) would put ECDSA signing on "
        "the live auth path — re-review the PYSEC-2026-1325 suppression in "
        "docs/SECURITY_ADVISORY_SUPPRESSIONS.md"
    )


def test_importing_the_apps_jose_surface_never_loads_ecdsa():
    """The strong form of the claim: ``ecdsa`` is not merely unused, it is never
    imported.

    Run in a subprocess on purpose — asserting ``'ecdsa' not in sys.modules``
    in-process is not hermetic under ``-n auto``, because any other test (or a
    transitive import from an unrelated dependency) could have loaded it first.
    ``import jose.jwt`` is exactly what ``app/core/security.py`` pulls in
    (``from jose import JWTError, jwt``), so this is the app's real import
    surface, and the subprocess costs ~0.1s.
    """
    probe = (
        "import sys\n"
        "import jose.jwt\n"
        "from jose.backends import ECKey\n"
        "loaded = sorted(m for m in sys.modules if m == 'ecdsa' or m.startswith('ecdsa.'))\n"
        # One delimited line: an empty module list must survive .strip().
        "print(ECKey.__module__ + '|' + ','.join(loaded))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, f"probe failed: {completed.stderr}"
    backend_module, loaded_ecdsa_modules = completed.stdout.strip().split("|", 1)
    assert backend_module == "jose.backends.cryptography_backend"
    assert loaded_ecdsa_modules == "", (
        f"importing the app's jose surface loaded {loaded_ecdsa_modules!r}; the vulnerable ecdsa code is "
        "now reachable — re-review the PYSEC-2026-1325 suppression in "
        "docs/SECURITY_ADVISORY_SUPPRESSIONS.md"
    )
