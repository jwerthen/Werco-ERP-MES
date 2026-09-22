"""Run bounded database follow-up checks away from the ARQ event loop."""

import asyncio


async def check_hank_watches_task():
    from app.services.hank_watch_service import process_hank_watches

    # The service creates/closes its sessions inside this worker thread and
    # commits each watch independently; no ORM session crosses thread boundaries.
    return await asyncio.to_thread(process_hank_watches, limit=100)
