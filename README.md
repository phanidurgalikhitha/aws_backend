# Secure Upload - Backend (AWS SAM, Python 3.12)

Serverless backend for the hackathon project. The mobile app asks this backend
**for permission** to upload (tiny JSON), then uploads the file **directly to S3**
with a short-lived presigned URL. The file never passes through any server of ours.

```
CONTROL PATH   App --JWT--> API Gateway --> Lambda (validate, build key, sign) --> App
DATA PATH      App ==== PUT file ====> S3 (private bucket)
POST-UPLOAD    S3 ObjectCreated --> Lambda (verify size/type/magic bytes) --> DynamoDB
```

## What gets created

| Service | Purpose |
|---|---|
| Cognito User Pool + app client | Sign up, email verification, login, JWT |
| API Gateway (HTTP API) + JWT authorizer | HTTPS front door, rejects requests without a valid token, throttling |
| 5 Lambda functions | presign, list, download-url, delete, process-upload |
| S3 bucket | Private (Block Public Access), encrypted, HTTPS-only, CORS for web testing |
| DynamoDB table | File metadata (`userId` + `fileId`), TTL cleans up abandoned uploads |
| CloudWatch alarms + SNS | Alerts on Lambda/API errors (optional email) |

## API

All routes need `Authorization: Bearer <Cognito ID token>`.

| Method | Path | Body / result |
|---|---|---|
| POST | `/uploads/presigned-url` | `{fileName, contentType, fileSize}` -> `{uploadUrl, method, headers, fileId, objectKey, expiresIn}` |
| GET | `/files?limit=50&nextToken=` | `{files:[...], nextToken?}` newest first |
| GET | `/files/{fileId}/download-url` | `{downloadUrl, expiresIn}` (only when status = UPLOADED) |
| DELETE | `/files/{fileId}` | `{deleted:true}` |

File status lifecycle: `PENDING` -> `UPLOADED` (or `REJECTED` + `rejectReason`).

## Prerequisites

- AWS account + AWS CLI configured (`aws configure`)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.12 (only for `sam build` / running tests)

## Deploy

```bash
sam build
sam deploy --guided
```

Suggested answers: stack name `secure-upload`, your region (e.g. `ap-south-1`),
accept the defaults, allow SAM to create IAM roles. Optional parameters:
`MaxFileSizeMB` (default 25), `AllowedContentTypes`, `UrlExpirySeconds`, `AlertEmail`.

When it finishes, copy the **Outputs**; the frontend `.env` needs `ApiUrl`, `Region`, `UserPoolClientId`.

## Test the backend without the app

```bash
REGION=ap-south-1
CLIENT_ID=<UserPoolClientId>
API=<ApiUrl>

# 1. Sign up + confirm (code arrives by email)
aws cognito-idp sign-up --region $REGION --client-id $CLIENT_ID \
  --username you@example.com --password 'Passw0rd1'
aws cognito-idp confirm-sign-up --region $REGION --client-id $CLIENT_ID \
  --username you@example.com --confirmation-code 123456

# 2. Log in and grab the ID token
TOKEN=$(aws cognito-idp initiate-auth --region $REGION --client-id $CLIENT_ID \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=you@example.com,PASSWORD='Passw0rd1' \
  --query 'AuthenticationResult.IdToken' --output text)

# 3. Ask for an upload URL
curl -s -X POST "$API/uploads/presigned-url" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"fileName":"hello.txt","contentType":"text/plain","fileSize":11}'

# 4. Upload straight to S3 (use uploadUrl from step 3; header must match exactly)
curl -X PUT "<uploadUrl>" -H "Content-Type: text/plain" --data-binary "hello world"

# 5. A second later the file should be UPLOADED
curl -s "$API/files" -H "Authorization: Bearer $TOKEN"
```

## Run unit tests locally

```bash
python -m unittest discover -s . -p "test_*.py" -t .
```
The tests use in-memory fakes for S3 and DynamoDB, so no AWS account is needed.

## Security design (good talking points)

- **No AWS keys in the app.** The Lambda role signs the URL; the app only receives a temporary signed link.
- **Server decides the object key** (`uploads/{userId}/{fileId}-{safeName}`). The userId comes from the verified JWT, so users cannot write into or read each other's folders.
- **Least privilege IAM**: each Lambda only gets the exact S3/DynamoDB actions it needs.
- **Bucket is private**, encrypted at rest, HTTPS-only, public access fully blocked.
- **URLs expire** (default 5 minutes) and are bound to one key, one method and one Content-Type.
- **Ownership checks** on every read/delete (DynamoDB key includes `userId`).
- **Honest limitations**: a presigned PUT can't hard-cap upload size, so `process-upload`
  verifies the real object (size, Content-Type, magic bytes for PDF/PNG/JPEG/GIF)
  and deletes anything that fails. For hard enforcement, switch to a presigned POST with
  a `content-length-range` condition. Content sniffing covers only common formats; production
  would add malware scanning (e.g. GuardDuty Malware Protection for S3).

## Clean up

```bash
aws s3 rm s3://<BucketName> --recursive
sam delete
```

## Upload notifications (CloudWatch alarm -> SNS -> email)

When a photo passes verification, `process-upload` writes a CloudWatch custom metric
(`SecureUpload/PhotosUploaded`, via Embedded Metric Format). The `PhotoUploadedAlarm`
alarm fires when the metric is >= 1 within 60 seconds and publishes to the SNS topic,
which emails the address you pass as `AlertEmail`.

```bash
sam build
sam deploy --parameter-overrides AlertEmail=you@example.com
```
Then open the **"AWS Notification - Subscription Confirmation"** email and click **Confirm subscription**
(no emails are sent until you do). Expect the alarm email 1-2 minutes after an upload.
Several uploads inside the same minute produce one email; the alarm returns to OK
by itself and fires again on the next upload.
