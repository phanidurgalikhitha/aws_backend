"""DELETE /files/{fileId}"""
import common


@common.api_handler
def handler(event, context):
    user_id = common.get_user_id(event)
    file_id = (event.get("pathParameters") or {}).get("fileId")
    item = common.get_owned_file(user_id, file_id)

    common.get_s3().delete_object(Bucket=common.BUCKET_NAME, Key=item["objectKey"])
    common.get_table().delete_item(Key={"userId": user_id, "fileId": file_id})
    common.logger.info("Deleted file user=%s file=%s", user_id, file_id)
    return common.response(200, {"deleted": True, "fileId": file_id})
