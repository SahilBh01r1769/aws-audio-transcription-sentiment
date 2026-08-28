"""
WebSocket $connect route.
Fires once when the browser opens the WebSocket connection.
Records the connection in DynamoDB so later stateless Lambda
invocations can look up accumulated state by connectionId.
"""
import os, sys
from connection_store import put_connection


def lambda_handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    put_connection(connection_id)
    return {"statusCode": 200, "body": "Connected"}
