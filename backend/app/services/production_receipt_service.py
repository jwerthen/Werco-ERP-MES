"""Claim serialization and immutable responses for production retries."""

import hashlib
import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app.db.locks import acquire_generator_lock
from app.models.production_receipt import ProductionReceipt


def production_request_hash(operation_id, operator_id, payload):
    canonical = jsonable_encoder(payload.model_dump(exclude={"request_id"}))
    raw = json.dumps([operation_id, operator_id, canonical], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def find_production_replay(db, company_id, operator_id, operation_id, payload):
    if not payload.request_id:
        return None
    # Transaction lock survives until the receipt AND all ledger effects commit.
    # The unique constraint remains a second guard; the key is company-scoped so
    # another badge cannot reuse an uncertain attempt under a different operator.
    acquire_generator_lock(db, f"production_receipt:{payload.request_id}", company_id)
    receipt = (
        db.query(ProductionReceipt)
        .filter(ProductionReceipt.company_id == company_id, ProductionReceipt.request_id == payload.request_id)
        .first()
    )
    if receipt is None:
        return None
    expected = production_request_hash(operation_id, operator_id, payload)
    if receipt.operator_id != operator_id or receipt.request_hash != expected:
        raise HTTPException(
            409, "This request ID already records a different production report. Check the saved report."
        )
    # Historical receipt: replay remains readable after checkout/completion or
    # deletion. It authorizes no new write and is bound to the original actor.
    return {**receipt.response, "replayed": True}


def record_production_receipt(db, company_id, operator_id, operation_id, time_entry_id, payload, response):
    if not payload.request_id:
        return
    response.update(request_id=payload.request_id, replayed=False)
    db.add(
        ProductionReceipt(
            company_id=company_id,
            operator_id=operator_id,
            operation_id=operation_id,
            time_entry_id=time_entry_id,
            request_id=payload.request_id,
            request_hash=production_request_hash(operation_id, operator_id, payload),
            response=jsonable_encoder(response),
        )
    )
