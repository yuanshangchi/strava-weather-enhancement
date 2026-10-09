# Strava webhook deployment

Prepared for profile `strava-weather-enhancement`, region `us-east-2`.
This directory is a deployment package, not a live deployment.

## Flow

Strava → public HTTP API → receiver Lambda → FIFO SQS → worker Lambda.
The worker fetches the exact activity ID, checks athlete ownership and outdoor
run eligibility, compares routes over 90 days, and saves a draft in DynamoDB.
`PublishEnabled=false` is the default. No LLM is involved.

The receiver only returns success after enqueueing. Strava expects a response
within two seconds; short SDK timeouts keep this path small. Cold-start latency
must still be measured during deployment. The queue delays processing by 60
seconds to allow Strava's activity data to settle. Failed jobs retry up to five
receives before entering the dead-letter queue. Visibility is 30 minutes to
back off API failures/rate limits. A CloudWatch alarm detects dead letters but
has no email destination configured yet.

## Before first deployment

1. AWS SAM CLI 1.167.0 is installed in `aws/.tools-venv`; cfn-lint 1.57.1 is in
   `aws/.lint-venv`. Separate environments are required because SAM pins an older
   internal cfn-lint version. Verify your existing AWS CLI login before deployment.
2. Create a Secrets Manager secret in us-east-2 containing `client_id`,
   `client_secret`, and `tokens` (the contents of local tokens.json including
   `scope`). Transfer these programmatically without printing them or putting
   them in shell arguments. Do not commit credentials. Use the default Secrets
   Manager encryption key, or add explicit KMS permissions for a custom key.
3. Create a separate webhook secret with two independently generated random
   URL-safe values: `key` and `verify_token` (at least 32 random bytes each).
4. Record the two secret ARNs and your athlete ID. ARNs are references, not
   secret values. Keep both secrets in the same region as the stack.

Cloud credentials become authoritative after migration. Do not run local
scripts that refresh the same Strava tokens concurrently with the worker.
All queue messages use one FIFO group to serialize refresh and processing;
do not invoke the worker directly or change grouping without adding a lock.

## Build and deployment

From the project directory:

```sh
.venv/bin/python aws/stage_code.py
aws/.lint-venv/bin/cfn-lint --template aws/template.yaml --regions us-east-2
cd aws
PATH="$(pwd)/../.venv/bin:$PATH" SAM_CLI_TELEMETRY=0 .tools-venv/bin/sam build
SAM_CLI_TELEMETRY=0 .tools-venv/bin/sam deploy --guided --profile strava-weather-enhancement --region us-east-2
```

Use stack name `strava-weather-enhancement`, enter the athlete ID and secret
ARNs, leave `SubscriptionId=0` and `PublishEnabled=false` for initial setup.
Review the CloudFormation change set before executing it. The webhook is
intentionally public: Strava cannot use an AWS IAM authorizer. No VPC or NAT
Gateway is needed. Services incur usage/storage charges according to your plan.

`stage_code.py` copies an explicit code allowlist. Never change CodeUri to the
project root: that would risk packaging local `.env`, tokens and activity files.

## Register and verify (after deployment)

- Construct the callback from WebhookBaseUrl plus `/strava/webhook/<key>`.
  Keep this full URL private; do not log query strings or incoming events.
- Test the GET handshake with `hub.mode=subscribe`, the correct
  `hub.verify_token`, and a test `hub.challenge`. Confirm JSON echo and latency.
- List existing Strava subscriptions first. Strava permits one per app; do not
  delete an existing subscription automatically.
- Register using POST `https://www.strava.com/api/v3/push_subscriptions` with
  form fields `client_id`, `client_secret`, `callback_url`, `verify_token`.
  A helper should load values at runtime rather than exposing secrets in logs.
- Update the stack's SubscriptionId with the returned ID. Until this update,
  event POSTs are rejected; perform setup before uploading a test activity.
- Upload a run, inspect the DynamoDB draft and CloudWatch status, and measure
  webhook response latency. Confirm no Strava description changed.
- Enable PublishEnabled only after reviewing the first draft. Existing previewed
  jobs remain preview-only; new activities are then published automatically.

## Reliability and limits

Strava does not sign POST payloads. The unpredictable path is a bearer secret;
owner/subscription filters and fetching activity ownership are additional
checks, not cryptographic signatures. API Gateway throttling limits requests.
The endpoint ignores activity update/delete and athlete events; it does not
implement deauthorization cleanup. On revocation, disable processing and remove
stored credentials/drafts manually. Activity privacy/deletion cleanup is also
manual in this personal prototype.

The worker stores the exact draft before PUT. On retry, it uses the same draft:
an already applied description is a no-op; changed user notes cause failure.
There is still a read/PUT race because Strava does not provide a conditional
update here. Persistent deduplication is per athlete/activity, even after SQS's
five-minute deduplication window. Previewed/published/skipped jobs are retained;
there is no automatic data expiry. Review and remove data when no longer needed.

Deleting the stack retains the queues and drafts table intentionally; these may
continue to incur storage costs. Secrets are externally managed and also remain.
Remove the Strava subscription before retiring its callback endpoint.

## Checks

From the project directory:

```sh
.venv/bin/python -m unittest discover -v
```

Tests use fake services and never call Strava, AWS, or publish descriptions.

## Optional Gemini narration

Add a `gemini_api_key` field alongside `client_id`, `client_secret`, and `tokens`
in the existing Strava secret to enable Gemini for new worker drafts. Without
that field the worker uses the deterministic weather template. The key is read
only at runtime, never logged or stored in a draft. It is preserved during
Strava token rotation. Package staging includes comparison_recap.py and
generate_recap.py; no extra Python dependencies are needed for Gemini REST calls.
Stored drafts are reused on retries, including template fallbacks. Adding a key
or changing code does not regenerate already previewed/published drafts.
After updating code, rebuild and deploy the package; local changes do not update
an existing AWS deployment.
