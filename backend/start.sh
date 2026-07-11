#!/bin/bash
alembic upgrade head
# --proxy-headers + --forwarded-allow-ips="*" so uvicorn honors X-Forwarded-Proto behind
# a TLS proxy; otherwise request.url.scheme stays http and trailing-slash 307 redirects
# downgrade to http:// (browsers refuse to follow from an https origin -> status 0).
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*
