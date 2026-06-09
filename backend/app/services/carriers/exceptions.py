"""Typed carrier-integration errors.

The service layer catches these to map provider failures onto clean HTTP
responses without leaking provider internals. ``CarrierError`` is the base; the
others are specific failure modes the service distinguishes.
"""


class CarrierError(Exception):
    """Base for all carrier-provider errors."""


class RateUnavailableError(CarrierError):
    """No usable rate could be obtained for the requested shipment."""


class LabelPurchaseError(CarrierError):
    """A label (or BOL) purchase failed at the provider."""


class NotSupportedError(CarrierError):
    """The requested capability is not implemented by this provider/mode.

    Raised, for example, by the EasyPost adapter's freight path (EasyPost LTL is
    an Enterprise feature that cannot be exercised here) and by the registry for
    a provider that has no concrete adapter yet (``zenkraft``).
    """


class AddressInvalidError(CarrierError):
    """The provider rejected an address as undeliverable / unverifiable."""


class WebhookVerificationError(CarrierError):
    """An inbound carrier webhook failed signature verification."""


class EgressDisabledError(CarrierError):
    """A customer-data-bearing carrier call was attempted while egress is OFF.

    SAFETY: every outbound call that transmits customer data is gated behind the
    per-company ``allow_carrier_egress`` flag (defaults FALSE). When it is off the
    service raises this and makes NO external call. A pure credential
    ``test-connection`` (sends no customer data) is exempt.
    """
