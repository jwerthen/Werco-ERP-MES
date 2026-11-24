"""Read-only summary of where persistent document bytes are stored.

Served on the unauthenticated ``GET /health/detailed`` and on the authenticated
``GET /api/v1/setup/health``. It names WHICH backend is configured and where it points,
never how to reach it: no access key, no secret, no full URL, no filesystem path.
"""

from typing import Literal, Optional

from pydantic import BaseModel


class StorageStatus(BaseModel):
    """Document-storage configuration as this API process sees it.

    ``backend`` is the backend for NEW writes (``STORAGE_BACKEND``). Rows written
    earlier under the other backend stay servable (dispatch is per stored reference).
    The worker reads its own environment, so this describes the API process only.
    """

    backend: Literal["local", "s3"]
    #: ``"durable"`` -- object storage; ``"volume"`` -- local disk on what looks like a
    #: mounted volume; ``"ephemeral"`` -- local disk with no mount found (lost on
    #: redeploy); ``"unknown"`` -- local disk whose mount state could not be read.
    durability: Literal["durable", "volume", "ephemeral", "unknown"]

    # s3 only (None for local).
    bucket: Optional[str] = None
    #: Hostname of ``S3_ENDPOINT_URL`` only (no scheme, port, path, query or userinfo).
    #: None means the AWS S3 default endpoint for ``region``.
    endpoint_host: Optional[str] = None
    region: Optional[str] = None

    # local only (None for s3).
    #: True when ``UPLOAD_DIR`` was set explicitly rather than left at its default.
    upload_dir_configured: Optional[bool] = None
    upload_dir_exists: Optional[bool] = None
    #: True when the upload path, or a directory above it other than ``/``, is a mount
    #: point. A heuristic: it says a volume is mounted, not that the volume persists.
    upload_dir_on_mounted_volume: Optional[bool] = None

    #: Fixed advisory text when stored documents may not survive a redeploy, else None.
    warning: Optional[str] = None
