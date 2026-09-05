import asyncio
import base64
import json
import os
import time

import boto3

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

from connection_store import (
    append_audio_chunk,
    append_transcript,
    begin_transcription,
    end_transcription,
    get_active_transcriptions,
    get_audio_byte_count,
    take_audio_buffer,
    take_audio_buffer_if_ready,
)

from sentiment_helper import analyze_sentiment
from logging_helper import log_event


AWS_REGION = os.environ["AWS_REGION"]

SAMPLE_RATE = int(
    os.environ.get(
        "MIC_SAMPLE_RATE",
        "16000",
    )
)

BYTES_PER_SAMPLE = 2
TARGET_SEGMENT_SECONDS = 6

TARGET_BUFFER_BYTES = (
    SAMPLE_RATE
    * BYTES_PER_SAMPLE
    * TARGET_SEGMENT_SECONDS
)

# Graceful-stop settings.
FINISH_WAIT_SECONDS = 20
BUFFER_SETTLE_INTERVAL = 0.15
BUFFER_STABLE_POLLS = 3


class _ResultCollector(
    TranscriptResultStreamHandler
):
    def __init__(self, output_stream):
        super().__init__(
            output_stream
        )

        self.finalized_segments: list[str] = []
        self.latest_partial = ""

    async def handle_transcript_event(
        self,
        transcript_event: TranscriptEvent,
    ):
        for result in (
            transcript_event
            .transcript
            .results
        ):
            if not result.alternatives:
                continue

            text = (
                result
                .alternatives[0]
                .transcript
                .strip()
            )

            if not text:
                continue

            if result.is_partial:
                self.latest_partial = text

            else:
                self.finalized_segments.append(
                    text
                )

                self.latest_partial = ""


async def _transcribe_audio_segment(
    pcm_bytes: bytes,
) -> dict:
    client = TranscribeStreamingClient(
        region=AWS_REGION
    )

    stream = (
        await client.start_stream_transcription(
            language_code="en-US",
            media_sample_rate_hz=SAMPLE_RATE,
            media_encoding="pcm",
        )
    )

    async def _write_audio():
        chunk_size = 1024

        for offset in range(
            0,
            len(pcm_bytes),
            chunk_size,
        ):
            await (
                stream
                .input_stream
                .send_audio_event(
                    audio_chunk=pcm_bytes[
                        offset:
                        offset + chunk_size
                    ]
                )
            )

        await (
            stream
            .input_stream
            .end_stream()
        )

    handler = _ResultCollector(
        stream.output_stream
    )

    await asyncio.gather(
        _write_audio(),
        handler.handle_events(),
    )

    return {
        "final_text": " ".join(
            handler.finalized_segments
        ).strip(),

        "partial_text": (
            handler.latest_partial
            .strip()
        ),
    }


def _push_to_browser(
    apigw_client,
    connection_id: str,
    payload: dict,
):
    apigw_client.post_to_connection(
        ConnectionId=connection_id,
        Data=json.dumps(
            payload
        ).encode("utf-8"),
    )


def _process_audio_segment(
    apigw,
    connection_id: str,
    segment_pcm: bytes,
    final_flush: bool = False,
) -> None:
    """
    Transcribe one claimed PCM segment, store it, analyze
    sentiment and push the result back to the browser.
    """

    if not segment_pcm:
        return

    segment_seconds = (
        len(segment_pcm)
        / (
            SAMPLE_RATE
            * BYTES_PER_SAMPLE
        )
    )

    print(
        "[audio-buffer] "
        f"transcribing "
        f"{len(segment_pcm)} bytes "
        f"({segment_seconds:.2f}s) "
        f"final_flush={final_flush}"
    )

    begin_transcription(
        connection_id
    )

    try:
        try:
            result = asyncio.run(
                _transcribe_audio_segment(
                    segment_pcm
                )
            )

        except Exception as exc:
            print(
                "[transcribe] "
                f"error: {exc}"
            )

            try:
                _push_to_browser(
                    apigw,
                    connection_id,
                    {
                        "type": "error",
                        "message": str(exc),
                    },
                )
            except Exception:
                pass

            return

        final_text = (
            result.get(
                "final_text",
                "",
            )
            .strip()
        )

        partial_text = (
            result.get(
                "partial_text",
                "",
            )
            .strip()
        )

        print(
            "[transcribe] "
            f"final={final_text!r} "
            f"partial={partial_text!r}"
        )

        if final_text:
            try:
                full_so_far = (
                    append_transcript(
                        connection_id,
                        final_text,
                    )
                )

            except Exception as exc:
                print(
                    "[transcript-store] "
                    f"error: {exc}"
                )

                full_so_far = final_text

            try:
                sentiment = (
                    analyze_sentiment(
                        final_text
                    )
                )

            except Exception as exc:
                print(
                    "[sentiment] "
                    f"error: {exc}"
                )

                sentiment = {
                    "label": "NEUTRAL",
                    "score": 0.0,
                }

            try:
                log_event(
                    source="live",
                    transcript=final_text,
                    sentiment_label=(
                        sentiment["label"]
                    ),
                    sentiment_score=(
                        sentiment["score"]
                    ),
                    sentiment_engine=(
                        "aws_comprehend"
                    ),
                    extra={
                        "connection_id": (
                            connection_id
                        ),
                        "chunk": True,
                        "buffered_segment": True,
                        "final_flush": (
                            final_flush
                        ),
                        "segment_seconds": round(
                            segment_seconds,
                            2,
                        ),
                    },
                )

            except Exception as exc:
                print(
                    "[logging] "
                    f"error: {exc}"
                )

            try:
                _push_to_browser(
                    apigw,
                    connection_id,
                    {
                        "type": (
                            "final_transcript"
                        ),
                        "text": final_text,
                        "full_transcript": (
                            full_so_far
                        ),
                        "sentiment_label": (
                            sentiment["label"]
                        ),
                        "sentiment_score": (
                            sentiment["score"]
                        ),
                        "segment_seconds": round(
                            segment_seconds,
                            2,
                        ),
                        "final_flush": (
                            final_flush
                        ),
                    },
                )

            except Exception as exc:
                print(
                    "[browser-push] "
                    f"error: {exc}"
                )

        elif partial_text:
            try:
                _push_to_browser(
                    apigw,
                    connection_id,
                    {
                        "type": (
                            "partial_transcript"
                        ),
                        "text": partial_text,
                    },
                )

            except Exception as exc:
                print(
                    "[browser-push] "
                    f"error: {exc}"
                )

        else:
            print(
                "[transcribe] "
                "segment produced no text"
            )

    finally:
        try:
            remaining = end_transcription(
                connection_id
            )

            print(
                "[transcribe] "
                f"active remaining={remaining}"
            )

        except Exception as exc:
            print(
                "[transcribe-counter] "
                f"decrement error: {exc}"
            )


def _wait_for_active_transcriptions(
    connection_id: str,
    timeout_seconds: float,
) -> bool:
    """
    Wait until any already-running six-second segment
    finishes before draining the last partial segment.

    This also keeps transcript ordering sensible.
    """

    deadline = (
        time.time()
        + timeout_seconds
    )

    while time.time() < deadline:
        active = (
            get_active_transcriptions(
                connection_id
            )
        )

        print(
            "[finish] "
            f"active_transcriptions={active}"
        )

        if active == 0:
            return True

        time.sleep(0.2)

    return False


def _wait_for_audio_buffer_to_settle(
    connection_id: str,
) -> None:
    """
    The browser has stopped producing audio, but the final
    one or two WebSocket Lambda invocations may still be
    writing their chunks into DynamoDB.

    Wait until the byte count stays unchanged across several
    polls before taking the final buffer.
    """

    previous = None
    stable_polls = 0

    for _ in range(12):
        current = get_audio_byte_count(
            connection_id
        )

        print(
            "[finish] "
            f"buffer bytes={current}"
        )

        if current == previous:
            stable_polls += 1
        else:
            stable_polls = 0

        if (
            stable_polls
            >= BUFFER_STABLE_POLLS
        ):
            return

        previous = current

        time.sleep(
            BUFFER_SETTLE_INTERVAL
        )


def _finish_session(
    apigw,
    connection_id: str,
) -> dict:
    print(
        "[finish] "
        f"requested for {connection_id}"
    )

    # First let a normal six-second transcription already
    # in progress finish and push its result to the browser.
    finished = (
        _wait_for_active_transcriptions(
            connection_id,
            FINISH_WAIT_SECONDS,
        )
    )

    if not finished:
        print(
            "[finish] timeout waiting "
            "for existing transcription"
        )

    # Allow the very last audio_chunk Lambda invocation(s)
    # to finish writing to DynamoDB.
    _wait_for_audio_buffer_to_settle(
        connection_id
    )

    # Atomically drain whatever is left.
    remaining_pcm = take_audio_buffer(
        connection_id
    )

    if remaining_pcm:
        remaining_seconds = (
            len(remaining_pcm)
            / (
                SAMPLE_RATE
                * BYTES_PER_SAMPLE
            )
        )

        print(
            "[finish] "
            f"processing final "
            f"{remaining_seconds:.2f}s"
        )

        _process_audio_segment(
            apigw,
            connection_id,
            remaining_pcm,
            final_flush=True,
        )

    else:
        print(
            "[finish] "
            "no remaining audio"
        )

    # Make sure the final segment has completed before
    # telling the browser it is safe to close the socket.
    _wait_for_active_transcriptions(
        connection_id,
        FINISH_WAIT_SECONDS,
    )

    try:
        _push_to_browser(
            apigw,
            connection_id,
            {
                "type": "session_complete",
            },
        )

    except Exception as exc:
        print(
            "[finish] "
            f"session_complete push failed: {exc}"
        )

    return {
        "statusCode": 200,
        "body": "Session finished",
    }


def lambda_handler(
    event,
    context,
):
    request_context = event[
        "requestContext"
    ]

    connection_id = request_context[
        "connectionId"
    ]

    apigw = boto3.client(
        "apigatewaymanagementapi",

        endpoint_url=(
            f"https://"
            f"{request_context['domainName']}/"
            f"{request_context['stage']}"
        ),

        region_name=AWS_REGION,
    )

    try:
        body = json.loads(
            event.get("body") or "{}"
        )

    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "body": "Invalid JSON",
        }

    action = body.get(
        "action"
    )

    if action != "audio_chunk":
        return {
            "statusCode": 200,
            "body": "OK",
        }

    # Reuse the existing audio_chunk API Gateway route
    # instead of adding another WebSocket route.
    if body.get(
        "finish_session"
    ):
        return _finish_session(
            apigw,
            connection_id,
        )

    audio_b64 = body.get(
        "audio"
    )

    if not audio_b64:
        return {
            "statusCode": 200,
            "body": "No audio",
        }

    try:
        incoming_pcm = (
            base64.b64decode(
                audio_b64,
                validate=True,
            )
        )

    except Exception as exc:
        print(
            "[audio] "
            f"invalid base64: {exc}"
        )

        return {
            "statusCode": 400,
            "body": "Invalid base64",
        }

    if not incoming_pcm:
        return {
            "statusCode": 200,
            "body": "Empty audio",
        }

    try:
        buffered_bytes = (
            append_audio_chunk(
                connection_id,
                incoming_pcm,
            )
        )

    except Exception as exc:
        print(
            "[audio-buffer] "
            f"append failed: {exc}"
        )

        try:
            _push_to_browser(
                apigw,
                connection_id,
                {
                    "type": "error",
                    "message": (
                        "Audio buffering failed"
                    ),
                },
            )
        except Exception:
            pass

        return {
            "statusCode": 200,
            "body": "Buffer error handled",
        }

    buffered_seconds = (
        buffered_bytes
        / (
            SAMPLE_RATE
            * BYTES_PER_SAMPLE
        )
    )

    print(
        "[audio-buffer] "
        f"connection={connection_id} "
        f"bytes={buffered_bytes}/"
        f"{TARGET_BUFFER_BYTES} "
        f"seconds={buffered_seconds:.2f}"
    )

    if (
        buffered_bytes
        < TARGET_BUFFER_BYTES
    ):
        return {
            "statusCode": 200,
            "body": "Buffered",
        }

    # Atomically claim a complete segment. If another
    # concurrent invocation already claimed it, this returns
    # b"" and this invocation simply exits.
    segment_pcm = (
        take_audio_buffer_if_ready(
            connection_id,
            TARGET_BUFFER_BYTES,
        )
    )

    if not segment_pcm:
        print(
            "[audio-buffer] "
            "segment already claimed "
            "by another invocation"
        )

        return {
            "statusCode": 200,
            "body": "Already claimed",
        }

    _process_audio_segment(
        apigw,
        connection_id,
        segment_pcm,
        final_flush=False,
    )

    return {
        "statusCode": 200,
        "body": "Processed",
    }
