"""The single swap point for carrier providers.

``get_provider`` maps a ``CarrierAccount`` to a concrete ``CarrierProvider``,
decrypting the stored API key in-memory. Adding a new aggregator is a one-line
change here -- the service layer, endpoints, models, and UI program only against
the ``CarrierProvider`` ABC and the normalized ``types``.
"""

from app.models.carrier_account import CarrierAccount
from app.services.carriers.base import CarrierProvider
from app.services.carriers.crypto import decrypt_secret
from app.services.carriers.easypost_adapter import EasyPostProvider
from app.services.carriers.exceptions import CarrierError, NotSupportedError


def get_provider(carrier_account: CarrierAccount) -> CarrierProvider:
    """Return the concrete provider for a carrier account.

    Reads ``carrier_account.provider``, decrypts the API key, and returns the
    matching adapter. Only ``"easypost"`` is implemented today; ``"zenkraft"``
    raises ``NotSupportedError`` (TODO: add the Zenkraft adapter for native
    FedEx Freight / LTL once its wire format is available).

    SECURITY: the decrypted key is passed straight into the adapter and is never
    logged or returned.
    """
    provider = (carrier_account.provider or "").strip().lower()

    if not carrier_account.encrypted_api_key:
        raise CarrierError(f"Carrier account {carrier_account.id} has no API key configured")

    if provider == "easypost":
        api_key = decrypt_secret(carrier_account.encrypted_api_key)
        carrier_refs = carrier_account.carrier_refs or {}
        return EasyPostProvider(api_key=api_key, carrier_refs=carrier_refs)

    if provider == "zenkraft":
        # TODO(freight): implement ZenkraftProvider(CarrierProvider) with native
        # FedEx Freight / LTL support (create_freight_shipment / buy_bol) and
        # register it here. EasyPost LTL is Enterprise-gated and unimplemented.
        raise NotSupportedError("Zenkraft provider is not yet implemented; only 'easypost' is available.")

    raise NotSupportedError(f"Unknown carrier provider: {carrier_account.provider!r}")
