"""
Structured logging to DynamoDB.

Every transcription event (live chunk, session summary, file upload)
writes one row here. The frontend reads these rows via the get_logs Lambda.

Floats must be Decimal for DynamoDB's boto3 resource API — a common
gotcha that bites most people on first encounter.
"""
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

LOGS_TABLE = os.environ["LOGS_TABLE_NAME"]
AWS_REGION = os.environ["AWS_REGION"]

_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_table = _dynamodb.Table(LOGS_TABLE)


def log_event(
    source: str,
    transcript: str,
    sentiment_label: str,
    sentiment_score: float,
    sentiment_engine: str,
    duration_seconds: float | None = None,
    extra: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    entry = {
        "log_id": str(uuid.uuid4()),
        "timestamp_utc": now.isoformat(),
        "timestamp_epoch": int(now.timestamp()),
        "source": source,
        "transcript": transcript,
        "sentiment_label": sentiment_label,
        "sentiment_score": Decimal(str(round(float(sentiment_score), 4))),
        "sentiment_engine": sentiment_engine,
    }
    if duration_seconds is not None:
        entry["duration_seconds"] = Decimal(str(round(float(duration_seconds), 2)))
    if extra:
        for k, v in extra.items():
            entry[k] = Decimal(str(v)) if isinstance(v, float) else v

    _table.put_item(Item=entry)
    return entry
