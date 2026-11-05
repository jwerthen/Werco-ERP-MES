"""Request ``Content-Type`` predicates shared by the body-size cap and the routes it protects.

The JSON body cap in ``app/main.py`` (``limit_json_body_size``) must cap exactly the bodies
the app will hand to ``json.loads``. If its idea of "a JSON body" is narrower than
FastAPI's, a label FastAPI parses but the cap does not recognise is an uncapped,
unauthenticated parse ahead of every route's auth -- the thing the cap exists to stop. So
the predicates live here, in one place, rather than as string checks in two modules.
"""

from __future__ import annotations

import email.message
from typing import Optional


def is_json_media_type(content_type: Optional[str]) -> bool:
    """True when FastAPI would parse a body with this ``Content-Type`` as JSON.

    Mirrors FastAPI's own rule (``fastapi/routing.py``, ``get_request_handler``): the
    header is parsed with ``email.message.Message`` -- case-insensitive, parameters
    ignored -- and the body is JSON when the media type is ``application/json`` or any
    ``application/*+json`` structured-syntax subtype (``application/merge-patch+json``,
    ``application/vnd.api+json``, ...). A missing header is not JSON here: the app runs
    FastAPI's default ``strict_content_type=True``, under which an unlabelled body is
    never parsed as JSON.
    """
    if not content_type:
        return False
    message = email.message.Message()
    message["content-type"] = content_type
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


def is_text_plain_media_type(content_type: Optional[str]) -> bool:
    """True when a ``Content-Type`` header value names ``text/plain`` (any parameters).

    ``navigator.sendBeacon`` sends a string body, or a ``text/plain`` Blob, as
    ``text/plain;charset=UTF-8``. Used by the crash-beacon route (which reads that label
    as JSON) and by the body cap (which caps it on that route), so the two agree on what
    counts. Deliberately a plain split rather than ``email.message``: that parser reports
    a malformed header (``garbage``) as ``text/plain``, and a malformed label must not be
    relabelled as JSON.
    """
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "text/plain"
