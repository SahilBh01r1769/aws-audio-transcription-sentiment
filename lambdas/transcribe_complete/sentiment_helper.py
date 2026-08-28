"""
AWS Comprehend sentiment analysis wrapper.

Returns a unified dict: {"label": str, "score": float, "raw": dict}
Labels: POSITIVE | NEGATIVE | NEUTRAL | MIXED
"""
import os
import boto3

AWS_REGION = os.environ["AWS_REGION"]
_comprehend = boto3.client("comprehend", region_name=AWS_REGION)


def analyze_sentiment(text: str) -> dict:
    if not text or not text.strip():
        return {"label": "NEUTRAL", "score": 0.0, "raw": None}

    # Comprehend limit is 5000 UTF-8 bytes; truncate defensively
    truncated = text.encode("utf-8")[:4900].decode("utf-8", errors="ignore")
    response = _comprehend.detect_sentiment(Text=truncated, LanguageCode="en")

    label = response["Sentiment"]
    score = response["SentimentScore"][label.capitalize()]
    return {"label": label, "score": float(score), "raw": response}
