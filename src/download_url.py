"""GET /files/{fileId}/download-url  -  short-lived presigned GET URL."""
import common
from common import ApiError


@common.api_handler
def handler(event, context):
    user_id = common.get_user_id(event)
    file_id = (event.get("pathParameters") or {}).get("fileId")
    item = common.get_owned_file(user_id, file_id)

    if item.get("status") != common.STATUS_UPLOADED:
        raise ApiError(409, "not_ready", "File is not available for download.")

    safe_name = common.safe_filename(item.get("fileName", "file"))
    url = common.get_s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": common.BUCKET_NAME,
            "Key": item["objectKey"],
            "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
        },
        ExpiresIn=common.URL_EXPIRY_SECONDS,
        HttpMethod="GET",
    )
    return common.response(200, {"downloadUrl": url, "expiresIn": common.URL_EXPIRY_SECONDS})
