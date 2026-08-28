"""
Shared DynamoDB helper for tracking active WebSocket connections.

API Gateway invokes a SEPARATE Lambda for every WebSocket event
($connect, message, $disconnect). Lambda has no memory between
invocations, so we persist per-connection state in DynamoDB keyed
by connectionId. A TTL field auto-expires stale rows if $disconnect
is ever missed (network drop, browser crash, etc.).
"""
import os
import time

import boto3

CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE_NAME"]
AWS_REGION = os.environ["AWS_REGION"]

_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_table = _dynamodb.Table(CONNECTIONS_TABLE)


def put_connection(connection_id: str) -> None:
    _table.put_item(Item={
        "connection_id": connection_id,
        "created_at": int(time.time()),
        "ttl": int(time.time()) + 3600,
        "accumulated_transcript": "",
    })


def delete_connection(connection_id: str) -> None:
    _table.delete_item(Key={"connection_id": connection_id})


def get_connection(connection_id: str) -> dict | None:
    resp = _table.get_item(Key={"connection_id": connection_id})
    return resp.get("Item")


def append_transcript(connection_id: str, text_fragment: str) -> str:
    """Atomically append a transcript fragment and return the updated full text."""
    resp = _table.update_item(
        Key={"connection_id": connection_id},
        UpdateExpression="SET accumulated_transcript = if_not_exists(accumulated_transcript, :empty) + :frag",
        ExpressionAttributeValues={":frag": " " + text_fragment, ":empty": ""},
        ReturnValues="UPDATED_NEW",
    )
    return resp["Attributes"]["accumulated_transcript"].strip()
