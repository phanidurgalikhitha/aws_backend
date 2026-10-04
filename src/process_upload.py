"""S3 ObjectCreated -> verify the real object, then mark it UPLOADED or REJECTED.

A presigned PUT cannot strictly enforce size or type up front, so we check what
actually landed in the bucket and delete anything that breaks the rules.
"""
from urllib.parse import unquote_plus

import common

# Magic bytes for formats we can verify cheaply.
SIGNATURES = {
    "application/pdf": [b"%PDF-"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/gif": [b"GIF87a", b"GIF89a"],
}


def sniff_matches(content_type, head):
    signatures = SIGNATURES.get(content_type)
    if not signatures:
        return True  # nothing to verify for this type
    return any(head.startswith(sig) for sig in signatures)


def _reject(user_id, file_id, key, reason):
    common.logger.warning("Rejecting %s: %s", key, reason)
    common.get_s3().delete_object(Bucket=common.BUCKET_NAME, Key=key)
    common.get_table().update_item(
        Key={"userId": user_id, "fileId": file_id},
        UpdateExpression="SET #s = :s, rejectReason = :r, expiresAt = :e",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": common.STATUS_REJECTED,
            ":r": reason,
            ":e": common.epoch_in(common.REJECTED_TTL_SECONDS),
        },
        ConditionExpression="attribute_exists(userId)",
    )


def process_record(record):
    key = unquote_plus(record["s3"]["object"]["key"])
    parsed = common.parse_object_key(key)
    if not parsed:
        common.logger.warning("Ignoring unexpected key: %s", key)
        return
    user_id, file_id = parsed

    item = common.get_table().get_item(Key={"userId": user_id, "fileId": file_id}).get("Item")
    if not item or item.get("objectKey") != key:
        # Object was not created through our presign flow -> remove it.
        common.logger.warning("No matching record for %s; deleting object", key)
        common.get_s3().delete_object(Bucket=common.BUCKET_NAME, Key=key)
        return

    s3 = common.get_s3()
    head = s3.head_object(Bucket=common.BUCKET_NAME, Key=key)
    size = head["ContentLength"]
    actual_type = common.normalize_content_type(head.get("ContentType", ""))

    if size <= 0:
        return _reject(user_id, file_id, key, "File is empty.")
    if size > common.MAX_FILE_SIZE_BYTES:
        return _reject(user_id, file_id, key, f"File exceeds the {common.limit_text()} limit.")
    if actual_type != item.get("contentType") or not common.content_type_allowed(actual_type):
        return _reject(user_id, file_id, key, "File type is not allowed.")

    first_bytes = s3.get_object(Bucket=common.BUCKET_NAME, Key=key, Range="bytes=0-15")["Body"].read()
    if not sniff_matches(actual_type, first_bytes):
        return _reject(user_id, file_id, key, "File content does not match its declared type.")

    common.get_table().update_item(
        Key={"userId": user_id, "fileId": file_id},
        UpdateExpression="SET #s = :s, fileSize = :size, uploadedAt = :t REMOVE expiresAt",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": common.STATUS_UPLOADED,
            ":size": size,
            ":t": common.now_iso(),
        },
        ConditionExpression="attribute_exists(userId)",
    )
    common.logger.info("Upload verified user=%s file=%s size=%s", user_id, file_id, size)
    common.emit_metric("FilesUploaded", userId=user_id, fileId=file_id)
    if actual_type.startswith("image/"):
        common.emit_metric("PhotosUploaded", userId=user_id, fileId=file_id)


def handler(event, context):
    failures = 0
    for record in event.get("Records", []):
        try:
            process_record(record)
        except Exception:  # pylint: disable=broad-except
            failures += 1
            common.logger.exception("Failed to process record")
    if failures:
        raise RuntimeError(f"{failures} record(s) failed")  # lets Lambda retry
    return {"processed": len(event.get("Records", []))}
