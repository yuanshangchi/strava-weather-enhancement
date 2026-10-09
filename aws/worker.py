"""Single-athlete FIFO worker. Drafts are persisted before attempting any write."""
import json
import os
import time
from functools import lru_cache

from preview_weather import fetch_json
from route_matches import is_run
from sync_description import prepare_draft, apply_draft


@lru_cache
def services():
    import boto3
    return (boto3.client('secretsmanager'),
            boto3.resource('dynamodb').Table(os.environ['JOBS_TABLE']))


def credentials(secret_client):
    # Runtime-only resolution; never print credentials or full API responses.
    data = json.loads(secret_client.get_secret_value(SecretId=os.environ['STRAVA_SECRET_ARN'])['SecretString'])
    tokens = data['tokens']
    if tokens['expires_at'] <= time.time() + 600:
        refreshed = fetch_json('https://www.strava.com/oauth/token', form={
            'client_id': data['client_id'], 'client_secret': data['client_secret'],
            'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']})
        if not all(refreshed.get(k) for k in ('access_token', 'refresh_token', 'expires_at')):
            raise RuntimeError('Incomplete token refresh')
        tokens.update(refreshed)
        secret_client.put_secret_value(SecretId=os.environ['STRAVA_SECRET_ARN'], SecretString=json.dumps(data))
    return tokens


def process(message, secret_client, table):
    athlete = int(os.environ['ATHLETE_ID'])
    activity_id = message['object_id']
    if message['owner_id'] != athlete or type(activity_id) is not int or activity_id <= 0:
        raise ValueError('Unexpected activity destination')
    key = f'{athlete}:{activity_id}'
    item = table.get_item(Key={'id': key}, ConsistentRead=True).get('Item', {})
    # Previewed jobs never become writes simply because publishing is enabled later.
    if item.get('status') in ('published', 'previewed', 'skipped'):
        return item['status']
    tokens = credentials(secret_client)
    token = tokens['access_token']
    if item.get('draft'):
        draft = json.loads(item['draft'])
    else:
        activity = fetch_json(f'https://www.strava.com/api/v3/activities/{activity_id}', token=token)
        if activity.get('athlete', {}).get('id') != athlete:
            raise RuntimeError('Activity ownership mismatch')
        if not is_run(activity):
            table.put_item(Item={'id': key, 'status': 'skipped'})
            return 'skipped'
        draft = prepare_draft(activity_id, token=token, activity=activity)
        # JSON string avoids DynamoDB float conversion and retains exact draft text.
        table.put_item(Item={'id': key, 'status': 'prepared', 'draft': json.dumps(draft)})
    publishing = os.environ.get('PUBLISH_ENABLED', 'false') == 'true'
    if publishing:
        # On retry this checks whether the exact saved draft was already applied;
        # intervening user edits cause a failure instead of being overwritten.
        apply_draft(draft, token=token, scopes=tokens.get('scope', []))
    status = 'published' if publishing else 'previewed'
    table.update_item(Key={'id': key}, UpdateExpression='SET #s = :s',
                      ExpressionAttributeNames={'#s': 'status'},
                      ExpressionAttributeValues={':s': status})
    return status


def handler(event, context):
    secret_client, table = services()
    # SAM config fixes batch size to one; FIFO group serializes token rotation.
    if len(event['Records']) != 1:
        raise ValueError('Expected one FIFO record')
    status = process(json.loads(event['Records'][0]['body']), secret_client, table)
    print(json.dumps({'status': status}))
