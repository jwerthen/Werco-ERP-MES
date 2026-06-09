"""Swappable multi-carrier provider abstraction.

The rest of the application only ever sees the normalized shapes in ``types`` and
the ``CarrierProvider`` ABC in ``base``; concrete aggregator wire formats
(EasyPost today, Zenkraft later) live behind ``registry.get_provider`` -- the
single swap point.
"""
