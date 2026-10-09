import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).parent / 'aws'))
import webhook
import worker

class CloudTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'WEBHOOK_KEY': 'key', 'VERIFY_TOKEN': 'verify',
            'ATHLETE_ID': '7', 'SUBSCRIPTION_ID': '12', 'QUEUE_URL': 'queue', 'PUBLISH_ENABLED': 'false'})
        env.start(); self.addCleanup(env.stop)

    def event(self, method='POST'):
        return {'requestContext': {'http': {'method': method}}, 'pathParameters': {'key': 'key'},
            'body': json.dumps({'object_type': 'activity', 'aspect_type': 'create',
                'object_id': 123, 'owner_id': 7, 'subscription_id': 12, 'event_time': 100})}

    def test_handshake(self):
        event = self.event('GET')
        event['queryStringParameters'] = {'hub.mode': 'subscribe', 'hub.verify_token': 'verify', 'hub.challenge': 'abc'}
        result = webhook.handler(event, None)
        self.assertEqual(result['statusCode'], 200)
        self.assertEqual(json.loads(result['body']), {'hub.challenge': 'abc'})
        event['queryStringParameters']['hub.verify_token'] = 'bad'
        self.assertEqual(webhook.handler(event, None)['statusCode'], 403)

    @patch.object(webhook, 'queue')
    def test_enqueue_and_failure(self, queue):
        self.assertEqual(webhook.handler(self.event(), None)['statusCode'], 200)
        args = queue.return_value.send_message.call_args.kwargs
        self.assertEqual(json.loads(args['MessageBody'])['object_id'], 123)
        queue.return_value.send_message.side_effect = RuntimeError('queue unavailable')
        with self.assertRaises(RuntimeError): webhook.handler(self.event(), None)

    @patch.object(webhook, 'queue')
    def test_ignore_updates_and_reject_invalid_requests(self, queue):
        for changes, status in [({'aspect_type': 'update'}, 200), ({'owner_id': 99}, 403),
                                ({'subscription_id': 2}, 403), ({'object_id': True}, 400)]:
            event = self.event(); body = json.loads(event['body']); body.update(changes)
            event['body'] = json.dumps(body)
            self.assertEqual(webhook.handler(event, None)['statusCode'], status)
        event['body'] = '[]'
        self.assertEqual(webhook.handler(event, None)['statusCode'], 400)
        event['pathParameters']['key'] = 'bad'
        self.assertEqual(webhook.handler(event, None)['statusCode'], 404)
        queue.assert_not_called()

    def test_preview_and_duplicate(self):
        table = Mock(); table.get_item.return_value = {}
        activity = {'id': 123, 'athlete': {'id': 7}}
        draft = {'activity_id': 123, 'athlete_id': 7, 'proposed_description': 'weather'}
        with patch.object(worker, 'credentials', return_value={'access_token': 'fake'}), \
             patch.object(worker, 'fetch_json', return_value=activity), \
             patch.object(worker, 'is_run', return_value=True), \
             patch.object(worker, 'prepare_draft', return_value=draft) as prepare, \
             patch.object(worker, 'apply_draft') as apply:
            message = {'object_id': 123, 'owner_id': 7}
            self.assertEqual(worker.process(message, Mock(), table), 'previewed')
            prepare.assert_called_once_with(123, token='fake', activity=activity)
            self.assertEqual(json.loads(table.put_item.call_args.kwargs['Item']['draft']), draft)
            table.get_item.return_value = {'Item': {'status': 'previewed'}}
            os.environ['PUBLISH_ENABLED'] = 'true'
            self.assertEqual(worker.process(message, Mock(), table), 'previewed')
            apply.assert_not_called()

    def test_retry_saved_draft_and_failure_not_marked_success(self):
        table = Mock(); draft = {'activity_id': 123, 'athlete_id': 7}
        table.get_item.return_value = {'Item': {'status': 'prepared', 'draft': json.dumps(draft)}}
        os.environ['PUBLISH_ENABLED'] = 'true'
        with patch.object(worker, 'credentials', return_value={'access_token': 'fake', 'scope': ['activity:write']}), \
             patch.object(worker, 'prepare_draft') as prepare, \
             patch.object(worker, 'apply_draft', side_effect=RuntimeError('unknown result')) as apply:
            with self.assertRaises(RuntimeError): worker.process({'object_id': 123, 'owner_id': 7}, Mock(), table)
            table.update_item.assert_not_called(); prepare.assert_not_called()
            apply.side_effect = None
            self.assertEqual(worker.process({'object_id': 123, 'owner_id': 7}, Mock(), table), 'published')
            self.assertEqual(apply.call_args.args[0], draft)

    def test_rotated_tokens_persist_with_scope(self):
        secret = Mock(); secret.get_secret_value.return_value = {'SecretString': json.dumps({
            'client_id': 'fake', 'client_secret': 'fake', 'tokens': {'expires_at': 0,
            'refresh_token': 'old', 'scope': ['activity:write']}})}
        with patch.dict(os.environ, {'STRAVA_SECRET_ARN': 'fake'}), patch.object(worker, 'fetch_json',
            return_value={'access_token': 'new', 'refresh_token': 'rotated', 'expires_at': 9999999999}):
            tokens = worker.credentials(secret)
        self.assertEqual(tokens['scope'], ['activity:write'])
        self.assertEqual(json.loads(secret.put_secret_value.call_args.kwargs['SecretString'])['tokens']['refresh_token'], 'rotated')

    def test_wrong_athlete_never_generates(self):
        table = Mock(); table.get_item.return_value = {}
        with patch.object(worker, 'credentials', return_value={'access_token': 'fake'}), \
             patch.object(worker, 'fetch_json', return_value={'athlete': {'id': 99}}), \
             patch.object(worker, 'prepare_draft') as prepare:
            with self.assertRaises(RuntimeError): worker.process({'object_id': 123, 'owner_id': 7}, Mock(), table)
            prepare.assert_not_called()

if __name__ == '__main__':
    unittest.main()
