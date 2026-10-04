"""Shared helpers for all Lambda functions."""
import base64
import functools
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "")
MAX_FILE_SIZE_BYTES = int(os.environ.get("MAX_FILE_SIZE_BYTES", str(25 * 1000 * 1000)))
URL_EXPIRY_SECONDS = int(os.environ.get("URL_EXPIRY_SECONDS", "300"))
ALLOWED_CONTENT_TYPES = [
    t.strip().lower()
    for t in os.environ.get("ALLOWED_CONTENT_TYPES", "image/*,application/pdf,text/plain").split(",")
    if t.strip()
]

KEY_PREFIX = "uploads/"
PENDING_TTL_SECONDS = 24 * 3600          # abandoned uploads vanish after a day
REJECTED_TTL_SECONDS = 7 * 24 * 3600

STATUS_PENDING = "PENDING"
STATUS_UPLOADED = "UPLOADED"
STATUS_REJECTED = "REJECTED"

_s3 = None
_table = None


def get_s3():
    """Regional SigV4 client. Needed so presigned URLs work for new buckets."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Not JSON serializable: {type(value)}")


def response(status, body=None):
    result = {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
    }
    if body is not None:
        result["body"] = json.dumps(body, default=_json_default)
    return result


def api_handler(fn):
    """Turns ApiError into clean 4xx responses and hides internals on 500."""

    @functools.wraps(fn)
    def wrapper(event, context):
        try:
            return fn(event, context)
        except ApiError as err:
            logger.info("Request rejected: %s %s", err.code, err.message)
            return response(err.status, {"error": err.code, "message": err.message})
        except Exception:  # pylint: disable=broad-except
            logger.exception("Unhandled error")
            return response(500, {"error": "internal_error", "message": "Something went wrong."})

    return wrapper


def get_user_id(event):
    """The user id comes from the JWT that API Gateway already verified."""
    try:
        sub = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        sub = None
    if not sub or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", str(sub)):
        raise ApiError(401, "unauthorized", "Missing or invalid user identity.")
    return str(sub)


def parse_body(event):
    raw = event.get("body")
    if not raw:
        raise ApiError(400, "invalid_request", "Request body is required.")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "invalid_request", "Body must be valid JSON.") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "Body must be a JSON object.")
    return data


def normalize_content_type(value):
    return str(value).split(";")[0].strip().lower()


def content_type_allowed(content_type):
    ct = normalize_content_type(content_type)
    for pattern in ALLOWED_CONTENT_TYPES:
        if pattern == ct:
            return True
        if pattern.endswith("/*") and ct.startswith(pattern[:-1]):
            return True
    return False


def safe_filename(name):
    """Strip paths and odd characters so the S3 key is always safe."""
    base = re.split(r"[\\/]", str(name))[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    if len(base) > 100:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 100 - len(ext) - 1] + dot + ext) if dot and len(ext) < 10 else base[:100]
    return base


def new_file_id():
    """32 hex chars: 12 of millisecond timestamp (sortable) + 20 random."""
    return f"{int(time.time() * 1000):012x}" + uuid.uuid4().hex[:20]


def build_object_key(user_id, file_id, file_name):
    return f"{KEY_PREFIX}{user_id}/{file_id}-{safe_filename(file_name)}"


KEY_PATTERN = re.compile(r"^uploads/([A-Za-z0-9-]{8,64})/([0-9a-f]{32})-(.+)$")


def parse_object_key(key):
    match = KEY_PATTERN.match(key)
    if not match:
        return None
    return match.group(1), match.group(2)


def limit_text():
    if MAX_FILE_SIZE_BYTES >= 1_000_000:
        return f"{MAX_FILE_SIZE_BYTES // 1_000_000} MB"
    return f"{MAX_FILE_SIZE_BYTES} bytes"


def emit_metric(name, **properties):
    """Publish a custom CloudWatch metric by printing one EMF JSON line."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "SecureUpload",
                            "Dimensions": [[]],
                            "Metrics": [{"Name": name, "Unit": "Count"}],
                        }
                    ],
                },
                name: 1,
                **properties,
            }
        )
    )


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_in(seconds):
    return int(time.time()) + seconds


def get_owned_file(user_id, file_id):
    """Fetch a file record only if it belongs to this user."""
    if not re.fullmatch(r"[0-9a-f]{32}", file_id or ""):
        raise ApiError(400, "invalid_request", "Invalid file id.")
    item = get_table().get_item(Key={"userId": user_id, "fileId": file_id}).get("Item")
    if not item:
        raise ApiError(404, "not_found", "File not found.")
    return item


def public_file(item):
    """Fields that are safe to send to the client."""
    return {
        "fileId": item["fileId"],
        "fileName": item.get("fileName"),
        "contentType": item.get("contentType"),
        "fileSize": item.get("fileSize", item.get("declaredSize")),
        "status": item.get("status"),
        "createdAt": item.get("createdAt"),
        "uploadedAt": item.get("uploadedAt"),
        "rejectReason": item.get("rejectReason"),
    }
