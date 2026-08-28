"""
REST POST /upload-url
Returns a presigned S3 URL the browser can PUT an audio file directly to.

Direct-to-S3 upload bypasses both API Gateway (10MB limit) and Lambda
(6MB limit) — audio files can easily exceed both.
"""
import json
import os
import uuid
import boto3
from botocore.config import Config

UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET_NAME"]
AWS_REGION = os.environ["AWS_REGION"]



_s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(s3={"addressing_style": "virtual"})
)
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}

ALLOWED_EXTENSIONS = {"wav", "mp3", "m4a", "flac", "ogg"}


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    ext = body.get("file_extension", "wav").lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        return {
            "statusCode": 400,
            "headers": CORS,
            "body": json.dumps({"error": f"Unsupported extension. Allowed: {sorted(ALLOWED_EXTENSIONS)}"}),
        }

    job_id = str(uuid.uuid4())
    object_key = f"uploads/{job_id}.{ext}"

    # ==================== FIXED PART ====================
    content_type = f"audio/{ext}" if ext in ["mp3", "m4a"] else "audio/wav"
    
    presigned_url = _s3.generate_presigned_url(
    "put_object",
    Params={
        "Bucket": UPLOAD_BUCKET,
        "Key": object_key,
        "ContentType": "application/octet-stream",
    },
    ExpiresIn=300,
)   


    print("UPLOAD_BUCKET =", UPLOAD_BUCKET)
    print("Generated URL =", presigned_url)
    # ====================================================

    return {
        "statusCode": 200,
        "headers": {**CORS, "Content-Type": "application/json"},
        "body": json.dumps({
            "job_id": job_id,
            "upload_url": presigned_url,
            "object_key": object_key,
        }),
    }
