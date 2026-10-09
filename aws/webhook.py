"""HTTP API receiver. No Strava/weather calls and no payload logging."""
import base64
import hashlib
import hmac
import json
import os
from functools import lru_cache


@lru_cache
def queue():
    import boto3
    from botocore.config import Config
    return boto3.client('sqs', config=Config(connect_timeout=0.4, read_timeout=0.8,
                                           retries={'total_max_attempts': 1}))


def response(status, body):
    return {'statusCode': status, 'headers': {'content-type': 'application/json'},
            'body': json.dumps(body)}


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method')
    # Strava does not sign POST events. A random callback path is an additional
    # bearer secret; do not log the URL. Owner/subscription IDs are not secrets.
    path_key = (event.get('pathParameters') or {}).get('key', '')
    if not hmac.compare_digest(path_key, os.environ['WEBHOOK_KEY']):
        return response(404, {})
    if method == 'GET':
        query = event.get('queryStringParameters') or {}
        if (query.get('hub.mode') != 'subscribe' or not query.get('hub.challenge')
                or not hmac.compare_digest(query.get('hub.verify_token', ''), os.environ['VERIFY_TOKEN'])):
            return response(403, {})
        return response(200, {'hub.challenge': query['hub.challenge']})
    if method != 'POST':
        return response(405, {})
    try:
        raw = event.get('body') or ''
        if len(raw) > 16384:
            return response(413, {})
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw, validate=True).decode()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Expected object')
        for key in ('owner_id', 'object_id', 'subscription_id', 'event_time'):
            if type(payload.get(key)) is not int or payload[key] <= 0:
                raise ValueError('Invalid ID or timestamp')
    except (ValueError, UnicodeError):
        return response(400, {})
    if (payload['owner_id'] != int(os.environ['ATHLETE_ID'])
            or payload['subscription_id'] != int(os.environ['SUBSCRIPTION_ID'])):
        return response(403, {})
    if payload.get('object_type') != 'activity' or payload.get('aspect_type') != 'create':
        return response(200, {'ignored': True})
    message = json.dumps({key: payload[key] for key in ('owner_id', 'object_id', 'event_time')}, sort_keys=True)
    # Only acknowledge after durable enqueue. Failures cause Strava to retry.
    queue().send_message(QueueUrl=os.environ['QUEUE_URL'], MessageBody=message,
                         MessageGroupId='personal-athlete',
                         MessageDeduplicationId=hashlib.sha256(message.encode()).hexdigest())
    return response(200, {'queued': True})
