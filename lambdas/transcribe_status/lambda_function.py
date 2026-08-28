"""
Triggered by S3 ObjectCreated on the uploads/ prefix.
Starts an AWS Transcribe batch job for the newly uploaded audio file.
Job completion is handled asynchronously by transcribe_complete via EventBridge.
"""
import os
import urllib.parse
import boto3

AWS_REGION = os.environ["AWS_REGION"]
OUTPUT_BUCKET = os.environ["TRANSCRIBE_OUTPUT_BUCKET"]

_transcribe = boto3.client("transcribe", region_name=AWS_REGION)

EXT_TO_FORMAT = {
    "wav": "wav", "mp3": "mp3",
    "m4a": "mp4", "flac": "flac", "ogg": "ogg",
}


def lambda_handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        # key looks like "uploads/<job_id>.<ext>"
        filename = key.rsplit("/", 1)[-1]
        job_id, _, ext = filename.rpartition(".")
        media_format = EXT_TO_FORMAT.get(ext.lower())

        if not job_id or not media_format:
            continue  # skip unrelated files

        _transcribe.start_transcription_job(
            TranscriptionJobName=f"audio-app-{job_id}",
            LanguageCode="en-US",
            MediaFormat=media_format,
            Media={"MediaFileUri": f"s3://{bucket}/{key}"},
            OutputBucketName=OUTPUT_BUCKET,
            OutputKey=f"transcripts/{job_id}.json",
        )

    return {"statusCode": 200}
