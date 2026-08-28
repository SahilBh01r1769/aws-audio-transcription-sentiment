"""
Triggered by EventBridge on "Transcribe Job State Change" (COMPLETED or FAILED).
Reads the Transcribe output JSON from S3, runs Comprehend sentiment,
logs the result to DynamoDB. The frontend's polling loop detects this row
by job_id and shows the result to the user.
"""
import json
import os
import sys

import boto3
from sentiment_helper import analyze_sentiment
from logging_helper import log_event

AWS_REGION = os.environ["AWS_REGION"]
OUTPUT_BUCKET = os.environ["TRANSCRIBE_OUTPUT_BUCKET"]

_s3 = boto3.client("s3", region_name=AWS_REGION)
_transcribe = boto3.client("transcribe", region_name=AWS_REGION)


def _job_id_from_name(job_name: str) -> str:
    prefix = "audio-app-"
    return job_name[len(prefix):] if job_name.startswith(prefix) else job_name


def lambda_handler(event, context):
    detail = event.get("detail", {})
    job_name = detail.get("TranscriptionJobName", "")
    status = detail.get("TranscriptionJobStatus", "")

    if not job_name:
        return {"statusCode": 400, "body": "Missing job name"}

    job_id = _job_id_from_name(job_name)

    if status == "FAILED":
        log_event(
            source="file", transcript="",
            sentiment_label="NEUTRAL", sentiment_score=0.0,
            sentiment_engine="none",
            extra={
                "job_id": job_id,
                "status": "FAILED",
                "failure_reason": detail.get("FailureReason", ""),
            },
        )
        return {"statusCode": 200, "body": "Logged failure"}

    if status != "COMPLETED":
        return {"statusCode": 200, "body": f"Ignoring status: {status}"}

    # Read Transcribe's output JSON from S3
    resp = _s3.get_object(Bucket=OUTPUT_BUCKET, Key=f"transcripts/{job_id}.json")
    transcript_json = json.loads(resp["Body"].read())
    transcript_text = transcript_json["results"]["transcripts"][0]["transcript"].strip()

    # Extract audio duration from the last word's end_time in the transcript
    duration_seconds = None
    items = transcript_json["results"].get("items", [])
    last_timed = next((i for i in reversed(items) if "end_time" in i), None)
    if last_timed:
        duration_seconds = float(last_timed["end_time"])

    sentiment = analyze_sentiment(transcript_text) if transcript_text else {"label": "NEUTRAL", "score": 0.0}

    entry = log_event(
        source="file",
        transcript=transcript_text,
        sentiment_label=sentiment["label"],
        sentiment_score=sentiment["score"],
        sentiment_engine="aws_comprehend",
        duration_seconds=duration_seconds,
        extra={"job_id": job_id, "status": "COMPLETED"},
    )

    return {"statusCode": 200, "body": json.dumps({"log_id": entry["log_id"]})}
