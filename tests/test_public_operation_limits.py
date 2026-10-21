from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.agent_public import _check_rate_limit


def test_map_and_chat_do_not_exhaust_the_analysis_operation_quota():
    host = str(uuid4())
    def request(path):
        return Request({"type":"http", "method":"POST", "path":path,
                        "headers":[], "client":(host,1)})
    _check_rate_limit(request('/map'), limit=1)
    _check_rate_limit(request('/analyses'), limit=1)
    _check_rate_limit(request('/chat'), limit=1)
    with pytest.raises(HTTPException) as error:
        _check_rate_limit(request('/analyses'), limit=1)
    assert error.value.status_code == 429
