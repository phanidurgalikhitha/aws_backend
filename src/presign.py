"""POST /uploads/presigned-url  -  control path only (tiny JSON, no file bytes)."""
import common
from common import ApiError


@common.api_handler
def handler(event, context):
    user_id = common.get_user_id(event)
    body = common.parse_body(event)

    file_name = body.get("fileName")
    content_type = body.get("contentType")
    file_size = body.get("fileSize")

    if not isinstance(file_name, str) or not 1 <= len(file_name.strip()) <= 255:
        raise ApiError(400, "invalid_file_name", "fileName must be 1-255 characters.")
    if not isinstance(content_type, str) or "/" not in content_type:
        raise ApiError(400, "invalid_content_type", "contentType is required, e.g. image/png.")
    if isinstance(file_size, bool) or not isinstance(file_size, int):
        raise ApiError(400, "invalid_file_size", "fileSize must be an integer number of bytes.")
    if file_size <= 0:
        raise ApiError(400, "invalid_file_size", "File is empty.")
    if file_size > common.MAX_FILE_SIZE_BYTES:
        raise ApiError(413, "file_too_large", f"File exceeds the {common.limit_text()} limit.")

    content_type = common.normalize_content_type(content_type)
    if not common.content_type_allowed(content_type):
        raise ApiError(415, "unsupported_type", f"Files of type {content_type} are not allowed.")

    # The server (not the client) decides the full object key.
    file_id = common.new_file_id()
    object_key = common.build_object_key(user_id, file_id, file_name)

    common.get_table().put_item(
        Item={
            "userId": user_id,
            "fileId": file_id,
            "objectKey": object_key,
            "fileName": file_name.strip()[:255],
            "contentType": content_type,
            "declaredSize": file_size,
            "status": common.STATUS_PENDING,
            "createdAt": common.now_iso(),
            "expiresAt": common.epoch_in(common.PENDING_TTL_SECONDS),
        },
        ConditionExpression="attribute_not_exists(fileId)",
    )

    # ContentType is part of the signature: S3 rejects an upload that sends a different one.
    upload_url = common.get_s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": common.BUCKET_NAME, "Key": object_key, "ContentType": content_type},
        ExpiresIn=common.URL_EXPIRY_SECONDS,
        HttpMethod="PUT",
    )
    common.logger.info("Issued upload URL user=%s file=%s size=%s", user_id, file_id, file_size)

    return common.response(
        200,
        {
            "uploadUrl": upload_url,
            "method": "PUT",
            "headers": {"Content-Type": content_type},
            "fileId": file_id,
            "objectKey": object_key,
            "expiresIn": common.URL_EXPIRY_SECONDS,
        },
    )
