"""Test helpers: env setup, boto3 stubs (only if boto3 isn't installed) and in-memory fakes."""
import os
import sys
import types

os.environ.update(
    BUCKET_NAME="test-bucket",
    TABLE_NAME="test-table",
    MAX_FILE_SIZE_BYTES="1000",
    ALLOWED_CONTENT_TYPES="image/*,application/pdf,text/plain",
    URL_EXPIRY_SECONDS="300",
    AWS_REGION="us-east-1",
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:  # pragma: no cover
    import boto3  # noqa: F401
except ImportError:  # sandbox without boto3
    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = lambda *a, **k: None
    boto3_mod.resource = lambda *a, **k: None
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **k: None
    sys.modules.update({"boto3": boto3_mod, "botocore": botocore, "botocore.config": botocore_config})

import common  # noqa: E402


class FakeTable:
    def __init__(self):
        self.items = {}
        self.updates = []

    def put_item(self, Item, **kwargs):
        self.items[(Item["userId"], Item["fileId"])] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["userId"], Key["fileId"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self.items.pop((Key["userId"], Key["fileId"]), None)

    def update_item(self, Key, **kwargs):
        self.updates.append((Key, kwargs))
        item = self.items[(Key["userId"], Key["fileId"])]
        values = kwargs["ExpressionAttributeValues"]
        item["status"] = values[":s"]
        if ":size" in values:
            item["fileSize"] = values[":size"]
            item.pop("expiresAt", None)
        if ":r" in values:
            item["rejectReason"] = values[":r"]

    def query(self, **kwargs):
        user = kwargs["ExpressionAttributeValues"][":u"]
        rows = sorted((v for (u, _), v in self.items.items() if u == user), key=lambda i: i["fileId"])
        if not kwargs.get("ScanIndexForward", True):
            rows.reverse()
        return {"Items": rows[: kwargs["Limit"]]}


class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}  # key -> (bytes, content_type)
        self.deleted = []
        self.presigned = []

    def generate_presigned_url(self, op, Params, ExpiresIn, HttpMethod):
        self.presigned.append((op, Params, ExpiresIn, HttpMethod))
        return f"https://test-bucket.s3.amazonaws.com/{Params['Key']}?X-Amz-Signature=fake"

    def head_object(self, Bucket, Key):
        data, ctype = self.objects[Key]
        return {"ContentLength": len(data), "ContentType": ctype}

    def get_object(self, Bucket, Key, Range=None):
        data, _ = self.objects[Key]
        return {"Body": FakeBody(data[:16])}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def install_fakes():
    common._table = FakeTable()
    common._s3 = FakeS3()
    return common._table, common._s3


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


def api_event(user=USER, body=None, path_params=None, query=None):
    import json

    event = {
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": user}}}} if user else {},
        "pathParameters": path_params,
        "queryStringParameters": query,
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event
