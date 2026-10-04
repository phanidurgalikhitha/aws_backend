"""GET /files  -  the caller's files, newest first."""
import base64
import json

import common
from common import ApiError


def _decode_token(token):
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
        if not isinstance(key, dict):
            raise ValueError
        return key
    except Exception as exc:  # pylint: disable=broad-except
        raise ApiError(400, "invalid_request", "Invalid nextToken.") from exc


@common.api_handler
def handler(event, context):
    user_id = common.get_user_id(event)
    params = event.get("queryStringParameters") or {}

    try:
        limit = max(1, min(int(params.get("limit", 50)), 100))
    except ValueError as exc:
        raise ApiError(400, "invalid_request", "limit must be a number.") from exc

    query = {
        "KeyConditionExpression": "userId = :u",
        "ExpressionAttributeValues": {":u": user_id},
        "ScanIndexForward": False,  # file ids start with a timestamp -> newest first
        "Limit": limit,
    }
    if params.get("nextToken"):
        start_key = _decode_token(params["nextToken"])
        if start_key.get("userId") != user_id:
            raise ApiError(400, "invalid_request", "Invalid nextToken.")
        query["ExclusiveStartKey"] = start_key

    result = common.get_table().query(**query)
    body = {"files": [common.public_file(i) for i in result.get("Items", [])]}
    if result.get("LastEvaluatedKey"):
        body["nextToken"] = base64.urlsafe_b64encode(
            json.dumps(result["LastEvaluatedKey"]).encode()
        ).decode()
    return common.response(200, body)
