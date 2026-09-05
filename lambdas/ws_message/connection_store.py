import base64
import os

import boto3
from botocore.exceptions import ClientError


CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE_NAME"]
AWS_REGION = os.environ["AWS_REGION"]

_dynamodb = boto3.resource(
    "dynamodb",
    region_name=AWS_REGION,
)

_table = _dynamodb.Table(CONNECTIONS_TABLE)


def get_connection(connection_id: str) -> dict | None:
    response = _table.get_item(
        Key={
            "connection_id": connection_id
        },
        ConsistentRead=True,
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

    return item.get(
        "accumulated_transcript",
        "",
    ).strip()


def append_audio_chunk(
    connection_id: str,
    pcm_bytes: bytes,
) -> int:
    """
    Append one small browser PCM frame to the temporary
    DynamoDB buffer.

    Returns total RAW PCM bytes buffered.
    """

    encoded_chunk = base64.b64encode(
        pcm_bytes
    ).decode("ascii")

    response = _table.update_item(
        Key={
            "connection_id": connection_id
        },
        UpdateExpression=(
            "SET audio_chunks = "
            "list_append("
            "if_not_exists(audio_chunks, :empty_list), "
            ":new_chunk"
            "), "
            "audio_bytes = "
            "if_not_exists(audio_bytes, :zero) + :chunk_size"
        ),
        ExpressionAttributeValues={
            ":empty_list": [],
            ":new_chunk": [encoded_chunk],
            ":zero": 0,
            ":chunk_size": len(pcm_bytes),
        },
        ReturnValues="UPDATED_NEW",
    )

    return int(
        response["Attributes"]["audio_bytes"]
    )


def _decode_chunks(
    encoded_chunks: list,
) -> bytes:
    if not encoded_chunks:
        return b""

    return b"".join(
        base64.b64decode(chunk)
        for chunk in encoded_chunks
    )


def take_audio_buffer_if_ready(
    connection_id: str,
    minimum_bytes: int,
) -> bytes:
    """
    Atomically claim and clear the audio buffer, but only
    if it has reached minimum_bytes.

    This prevents two concurrent Lambda invocations around
    the six-second threshold from both transcribing the same
    buffered audio.
    """

    try:
        response = _table.update_item(
            Key={
                "connection_id": connection_id
            },
            UpdateExpression=(
                "SET audio_chunks = :empty, "
                "audio_bytes = :zero"
            ),
            ConditionExpression=(
                "audio_bytes >= :minimum"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":zero": 0,
                ":minimum": minimum_bytes,
            },
            ReturnValues="ALL_OLD",
        )

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            return b""

        raise

    old_item = response.get(
        "Attributes",
        {},
    )

    return _decode_chunks(
        old_item.get(
            "audio_chunks",
            [],
        )
    )


def take_audio_buffer(
    connection_id: str,
) -> bytes:
    """
    Atomically take whatever audio remains and clear the
    server-side buffer.

    Used when the user presses Stop.
    """

    response = _table.update_item(
        Key={
            "connection_id": connection_id
        },
        UpdateExpression=(
            "SET audio_chunks = :empty, "
            "audio_bytes = :zero"
        ),
        ExpressionAttributeValues={
            ":empty": [],
            ":zero": 0,
        },
        ReturnValues="ALL_OLD",
    )

    old_item = response.get(
        "Attributes",
        {},
    )

    return _decode_chunks(
        old_item.get(
            "audio_chunks",
            [],
        )
    )


def get_audio_byte_count(
    connection_id: str,
) -> int:
    item = get_connection(
        connection_id
    ) or {}

    return int(
        item.get(
            "audio_bytes",
            0,
        )
    )


def begin_transcription(
    connection_id: str,
) -> int:
    response = _table.update_item(
        Key={
            "connection_id": connection_id
        },
        UpdateExpression=(
            "ADD active_transcriptions :one"
        ),
        ExpressionAttributeValues={
            ":one": 1,
        },
        ReturnValues="UPDATED_NEW",
    )

    return int(
        response["Attributes"].get(
            "active_transcriptions",
            0,
        )
    )


def end_transcription(
    connection_id: str,
) -> int:
    response = _table.update_item(
        Key={
            "connection_id": connection_id
        },
        UpdateExpression=(
            "ADD active_transcriptions :minus_one"
        ),
        ExpressionAttributeValues={
            ":minus_one": -1,
        },
        ReturnValues="UPDATED_NEW",
    )

    return max(
        0,
        int(
            response["Attributes"].get(
                "active_transcriptions",
                0,
            )
        ),
    )


def get_active_transcriptions(
    connection_id: str,
) -> int:
    item = get_connection(
        connection_id
    ) or {}

    return max(
        0,
        int(
            item.get(
                "active_transcriptions",
                0,
            )
        ),
    )
