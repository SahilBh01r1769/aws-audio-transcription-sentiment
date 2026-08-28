import asyncio
import base64
import json
import os
import sys

import boto3
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

# Add helpers to path
from connection_store import append_transcript
from sentiment_helper import analyze_sentiment
from logging_helper import log_event

AWS_REGION = os.environ["AWS_REGION"]
SAMPLE_RATE = int(os.environ.get("MIC_SAMPLE_RATE", "16000"))


class _ResultCollector(TranscriptResultStreamHandler):
    def __init__(self, output_stream):
        super().__init__(output_stream)
        self.finalized_segments: list[str] = []
        self.latest_partial: str = ""

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        for result in transcript_event.transcript.results:
            if not result.alternatives:
                continue
            text = result.alternatives[0].transcript
            if result.is_partial:
                self.latest_partial = text
            else:
                self.finalized_segments.append(text)
                self.latest_partial = ""


async def _transcribe_audio_chunk(pcm_bytes: bytes) -> dict:
    client = TranscribeStreamingClient(region=AWS_REGION)
    stream = await client.start_stream_transcription(
        language_code="en-US",
        media_sample_rate_hz=SAMPLE_RATE,
        media_encoding="pcm",
    )

    async def _write_chunks():
        for i in range(0, len(pcm_bytes), 1024):
            await stream.input_stream.send_audio_event(audio_chunk=pcm_bytes[i:i + 1024])
        await stream.input_stream.end_stream()

    handler = _ResultCollector(stream.output_stream)
    await asyncio.gather(_write_chunks(), handler.handle_events())

    return {
        "final_text": " ".join(handler.finalized_segments).strip(),
        "partial_text": handler.latest_partial,
    }


def _push_to_browser(apigw_client, connection_id: str, payload: dict):
    apigw_client.post_to_connection(
        ConnectionId=connection_id,
        Data=json.dumps(payload).encode("utf-8"),
    )


def lambda_handler(event, context):
    ctx = event["requestContext"]
    connection_id = ctx["connectionId"]

    apigw = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=f"https://{ctx['domainName']}/{ctx['stage']}",
        region_name=AWS_REGION,
    )

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "Invalid JSON"}

    if body.get("action") != "audio_chunk":
        return {"statusCode": 200, "body": "OK"}

    audio_b64 = body.get("audio")
    if not audio_b64:
        return {"statusCode": 200, "body": "No audio"}

    try:
        pcm_bytes = base64.b64decode(audio_b64, validate=True)
    except Exception:
        return {"statusCode": 400, "body": "Invalid base64"}

    if len(pcm_bytes) == 0:
        return {"statusCode": 200, "body": "Empty audio"}

    try:
        result = asyncio.run(_transcribe_audio_chunk(pcm_bytes))
    except Exception as exc:
        _push_to_browser(apigw, connection_id, {"type": "error", "message": str(exc)})
        return {"statusCode": 200, "body": "Transcription error handled"}

    if result["final_text"]:
        full_so_far = append_transcript(connection_id, result["final_text"])

        try:
            sentiment = analyze_sentiment(result["final_text"])
        except Exception:
            sentiment = {"label": "NEUTRAL", "score": 0.0}

        try:
            log_event(
                source="live",
                transcript=result["final_text"],
                sentiment_label=sentiment["label"],
                sentiment_score=sentiment["score"],
                sentiment_engine="aws_comprehend",
                extra={"connection_id": connection_id, "chunk": True},
            )
        except Exception:
            pass

        _push_to_browser(apigw, connection_id, {
            "type": "final_transcript",
            "text": result["final_text"],
            "full_transcript": full_so_far,
            "sentiment_label": sentiment["label"],
            "sentiment_score": sentiment["score"],
        })

    elif result["partial_text"]:
        _push_to_browser(apigw, connection_id, {
            "type": "partial_transcript",
            "text": result["partial_text"],
        })

    return {"statusCode": 200, "body": "Processed"}