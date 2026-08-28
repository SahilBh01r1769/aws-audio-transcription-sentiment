"""
REST GET /logs
Returns recent log entries from DynamoDB, newest first.
Used by the log history tab and by the file-upload tab's polling loop
(which looks for its specific job_id to appear in the results).
"""
import json
import os
from decimal import Decimal

import boto3

LOGS_TABLE = os.environ.get["LOGS_TABLE_NAME"]
AWS_REGION = os.environ["AWS_REGION"]

_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_table = _dynamodb.Table(LOGS_TABLE)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,GET",
}


def _decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Not serializable: {type(obj)}")


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    try:
        limit = min(int(params.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50

    # Scan + sort in memory — fine at demo/portfolio scale.
    # For production at high volume, add a GSI with timestamp_epoch as
    # sort key and use Query instead.
    response = _table.scan(Limit=500)
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("timestamp_epoch", 0), reverse=True)
    items = items[:limit]

    return {
        "statusCode": 200,
        "headers": {**CORS, "Content-Type": "application/json"},
        "body": json.dumps({"logs": items, "count": len(items)}, default=_decimal_to_float),
    }
