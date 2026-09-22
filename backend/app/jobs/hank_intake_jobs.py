"""Request-driven intake analysis; session ownership stays inside the worker thread."""

import asyncio


async def process_hank_intake_file_task(file_id: int):
    from app.services.hank_intake_service import process_intake_file

    return await asyncio.to_thread(process_intake_file, file_id)
