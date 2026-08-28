import os
import sys
from connection_store import get_full_transcript, delete_connection
from sentiment_helper import analyze_sentiment
from logging_helper import log_event


def lambda_handler(event, context):
    connection_id = event["requestContext"]["connectionId"]

    try:
        transcript = get_full_transcript(connection_id)

        if transcript:
            try:
                sentiment = analyze_sentiment(transcript)
            except Exception as exc:
                print("Session sentiment failed:", exc)
                sentiment = {
                    "label": "NEUTRAL",
                    "score": 0.0,
                }

            try:
                log_event(
                    source="live",
                    transcript=transcript,
                    sentiment_label=sentiment["label"],
                    sentiment_score=sentiment["score"],
                    sentiment_engine="aws_comprehend",
                    extra={
                        "connection_id": connection_id,
                        "chunk": False,
                        "session_summary": True,
                    },
                )
            except Exception as exc:
                print("Session summary logging failed:", exc)

    finally:
        # Always remove the WebSocket connection record
        delete_connection(connection_id)

    return {
        "statusCode": 200,
        "body": "Disconnected",
    }