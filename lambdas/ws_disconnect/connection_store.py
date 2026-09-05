import os
import time

import boto3


CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE_NAME"]
AWS_REGION = os.environ["AWS_REGION"]


_dynamodb = boto3.resource(
    "dynamodb",
    region_name=AWS_REGION,
)

_table = _dynamodb.Table(CONNECTIONS_TABLE)


def put_connection(connection_id: str) -> None:
    _table.put_item(
        Item={
            "connection_id": connection_id,
            "created_at": int(time.time()),
            "ttl": int(time.time()) + 3600,
            "transcript_fragments": [],
        }
    )


def delete_connection(connection_id: str) -> None:
    _table.delete_item(
        Key={
            "connection_id": connection_id
        }
    )


def get_connection(connection_id: str) -> dict | None:
    response = _table.get_item(
        Key={
            "connection_id": connection_id
        }
    )

    return response.get("Item")


def append_transcript(
    connection_id: str,
    text_fragment: str,
) -> str:
    response = _table.update_item(
        Key={
            "connection_id": connection_id
        },
        UpdateExpression=(
            "SET transcript_fragments = "
            "list_append("
            "if_not_exists(transcript_fragments, :empty), "
            ":fragment"
            ")"
        ),
        ExpressionAttributeValues={
            ":empty": [],
            ":fragment": [text_fragment],
        },
        ReturnValues="ALL_NEW",
    )

    fragments = response["Attributes"].get(
        "transcript_fragments",
        [],
    )

    return " ".join(fragments).strip()


def get_full_transcript(
    connection_id: str,
) -> str:
    item = get_connection(connection_id) or {}

    fragments = item.get(
        "transcript_fragments",
        [],
    )

    if fragments:
        return " ".join(fragments).strip()

    # Compatibility with older connection records.
    return item.get(
        "accumulated_transcript",
        "",
    ).strip()
