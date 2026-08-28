# Signal — AWS Audio Transcription & Sentiment

Fully serverless audio transcription and sentiment analysis app.
No server to manage, true scale-to-zero, everything runs on managed AWS services.

## Architecture

```
LIVE MIC
Browser mic → WebSocket → API Gateway WS → Lambda (ws_message)
                                             → Transcribe Streaming
                                             → Comprehend
                                             → DynamoDB log
                                             → transcript + sentiment → browser

FILE UPLOAD
Browser → POST /upload-url → Lambda → presigned S3 URL
Browser → PUT file → S3
S3 ObjectCreated → Lambda (transcribe_status) → Transcribe batch job
EventBridge (job complete) → Lambda (transcribe_complete) → Comprehend → DynamoDB
Browser polls GET /logs until job_id appears → shows result
```

## Lambda Functions

| Function | Trigger | Role |
|---|---|---|
| `ws_connect` | WS $connect | Record connection in DynamoDB |
| `ws_disconnect` | WS $disconnect | Log session summary, clean up |
| `ws_message` | WS message (audio_chunk) | Transcribe chunk, score sentiment, push back |
| `upload_url` | REST POST /upload-url | Generate presigned S3 URL |
| `transcribe_status` | S3 ObjectCreated | Start Transcribe batch job |
| `transcribe_complete` | EventBridge | Read transcript, score sentiment, log |
| `get_logs` | REST GET /logs | Return recent DynamoDB log entries |

## Deploy

```bash
# Prerequisites: AWS CLI configured, Python 3.12, pip
aws configure   # if not already done

# From the project root:
bash infra/deploy.sh
```

The script creates all resources and prints the frontend URL at the end.
Open that URL to use the app.

## Tear down

```bash
bash infra/teardown.sh
```

## Local development / testing

```bash
pip install boto3 amazon-transcribe==0.6.4 awscrt~=0.26.1
cd lambdas
python3 -m pytest tests/ -v     # after adding tests
```

## Cost notes (ap-south-1, pay-per-use, no idle cost)

- **Lambda**: ~$0.20 per 1M invocations
- **API Gateway WebSocket**: $1.00 per 1M messages
- **Transcribe Streaming**: ~$0.024 per minute of audio
- **Transcribe Batch**: ~$0.024 per minute of audio
- **Comprehend DetectSentiment**: $0.0001 per request (first 10M/month)
- **DynamoDB**: $1.25 per million write request units (PAY_PER_REQUEST)

Everything scales to $0 when idle.
