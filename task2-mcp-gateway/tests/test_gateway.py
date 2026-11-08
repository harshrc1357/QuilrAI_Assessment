"""Gateway HTTP seam: the real Starlette app, called in-process, with a recording downstream."""

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx2 as httpx
import jwt
import pytest

from mcp_gateway.app import build_app
from mcp_gateway.config import GatewaySettings

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SECRET = "test-secret-that-is-long-enough-for-hs256!"
DOWNSTREAM_URL = "http://downstream.invalid/mcp"
GATEWAY_URL = "http://gateway.test/mcp"


def make_token(role: str | None = "viewer", *, secret: str = SECRET, expires_in: int = 300) -> str:
    claims: dict[str, object] = {"sub": "user-1", "exp": int(time.time()) + expires_in}
    if role is not None:
        claims["role"] = role
    return jwt.encode(claims, secret, algorithm="HS256")


def bearer(
    role: str | None = "viewer", *, secret: str = SECRET, expires_in: int = 300
) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(role, secret=secret, expires_in=expires_in)}"}


def rpc(method: str, params: dict | None = None, id_: object = 1) -> dict:
    message: dict[str, object] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return message


def echo_reply(request: httpx.Request) -> httpx.Response:
    message = json.loads(request.content)
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": message.get("id"), "result": {"echo": message}},
        headers={"mcp-session-id": "session-abc"},
    )


@dataclass
class RecordingDownstream:
    """Stands in for the downstream MCP server and records every request it receives."""

    requests: list[httpx.Request] = field(default_factory=list)
    reply: Callable[[httpx.Request], httpx.Response] = echo_reply

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)


@pytest.fixture
def downstream() -> RecordingDownstream:
    return RecordingDownstream()


@pytest.fixture
def gateway_settings() -> GatewaySettings:
    """A test can replace these with `@pytest.mark.parametrize("gateway_settings", [...])`."""
    return GatewaySettings(downstream_url=DOWNSTREAM_URL, jwt_secret=SECRET)


@pytest.fixture
async def gateway(
    gateway_settings: GatewaySettings, downstream: RecordingDownstream
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(downstream.handle)
    async with httpx.AsyncClient(transport=transport) as downstream_client:
        app = build_app(gateway_settings, downstream_client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            yield client


async def test_tools_list_is_forwarded_and_the_downstream_reply_returned(gateway, downstream):
    response = await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=bearer("viewer"))

    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"echo": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}},
    }
    assert len(downstream.requests) == 1
    assert str(downstream.requests[0].url) == DOWNSTREAM_URL


@pytest.mark.parametrize(
    "raw_name",
    [
        pytest.param('"admin_reset_key"', id="plain"),
        pytest.param('"admin_"', id="bare-prefix"),
        # The gateway fails closed on case: a downstream that matches names without case
        # must not turn "ADMIN_reset_key" into a way around the check.
        pytest.param('"ADMIN_reset_key"', id="upper-case"),
        pytest.param('"Admin_Reset_Key"', id="mixed-case"),
        # A JSON escape decodes to the same name that the downstream server sees.
        pytest.param(r'"\u0061dmin_reset_key"', id="json-unicode-escape"),
    ],
)
async def test_viewer_calling_an_admin_tool_is_blocked_without_calling_downstream(
    gateway, downstream, raw_name
):
    body = (
        '{"jsonrpc": "2.0", "id": 7, "method": "tools/call",'
        f' "params": {{"name": {raw_name}, "arguments": {{}}}}}}'
    )

    response = await gateway.post(GATEWAY_URL, content=body, headers=bearer("viewer"))

    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert downstream.requests == []


@pytest.mark.parametrize(
    ("role", "tool_name"),
    [
        ("admin", "admin_reset_key"),
        ("admin", "get_weather"),
        ("viewer", "get_weather"),
        # Only the prefix counts: "admin" elsewhere in the name is not an admin tool.
        ("viewer", "get_admin_contact"),
        ("viewer", "admin"),
    ],
)
async def test_permitted_tool_calls_are_forwarded(gateway, downstream, role, tool_name):
    message = rpc("tools/call", {"name": tool_name, "arguments": {}})

    response = await gateway.post(GATEWAY_URL, json=message, headers=bearer(role))

    assert response.status_code == 200
    assert response.json()["result"]["echo"] == message
    assert len(downstream.requests) == 1


def _unsigned_token(role: str) -> str:
    """A token with alg "none": anyone can forge it, so it must never be accepted."""
    return jwt.encode({"role": role, "exp": int(time.time()) + 300}, key=None, algorithm="none")


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-authorization-header"),
        pytest.param({"Authorization": ""}, id="empty-header"),
        pytest.param({"Authorization": "Bearer"}, id="scheme-without-token"),
        pytest.param({"Authorization": "Bearer "}, id="blank-token"),
        pytest.param({"Authorization": f"Basic {make_token('admin')}"}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer not-a-jwt"}, id="malformed-token"),
        pytest.param(bearer("admin", secret="some-other-secret-of-enough-length!!"), id="bad-sig"),
        pytest.param(bearer("admin", expires_in=-60), id="expired"),
        pytest.param(bearer(None), id="no-role-claim"),
        pytest.param(bearer("superuser"), id="unknown-role"),
        pytest.param({"Authorization": f"Bearer {_unsigned_token('admin')}"}, id="alg-none"),
    ],
)
async def test_requests_without_a_valid_token_are_rejected_before_downstream(
    gateway, downstream, headers
):
    response = await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32000, "message": "Authentication required"},
    }
    assert downstream.requests == []


async def test_bearer_scheme_is_case_insensitive(gateway, downstream):
    headers = {"Authorization": f"bearer {make_token('viewer')}"}

    response = await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=headers)

    assert response.status_code == 200
    assert len(downstream.requests) == 1


async def test_an_admin_tool_call_sent_as_a_notification_is_still_blocked(gateway, downstream):
    message = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}

    response = await gateway.post(GATEWAY_URL, json=message, headers=bearer("viewer"))

    # MCP over HTTP: a refused notification gets an HTTP error status; its body has no id.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == -32001
    assert response.json()["id"] is None
    assert downstream.requests == []


@pytest.mark.parametrize(
    "client_response",
    [
        pytest.param({"jsonrpc": "2.0", "id": 4, "result": {"roots": []}}, id="result"),
        pytest.param(
            {"jsonrpc": "2.0", "id": "s-1", "error": {"code": -1, "message": "declined"}},
            id="error",
        ),
    ],
)
async def test_client_responses_to_server_requests_are_forwarded(
    gateway, downstream, client_response
):
    # MCP clients POST a JSON-RPC response to answer sampling, elicitation and roots requests.
    downstream.reply = lambda _request: httpx.Response(202)

    response = await gateway.post(GATEWAY_URL, json=client_response, headers=bearer("viewer"))

    assert response.status_code == 202
    assert [json.loads(r.content) for r in downstream.requests] == [client_response]


async def test_other_methods_and_notifications_are_forwarded(gateway, downstream):
    initialize = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}

    for message in (initialize, initialized):
        response = await gateway.post(GATEWAY_URL, json=message, headers=bearer("viewer"))
        assert response.status_code == 200

    assert [json.loads(r.content) for r in downstream.requests] == [initialize, initialized]


def _parse_error() -> dict:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty-body"),
        pytest.param(b"{not json", id="truncated"),
        pytest.param(b"\xff\xfe{}", id="not-utf8"),
        pytest.param(b'{"jsonrpc": "2.0", "id": NaN, "method": "tools/list"}', id="nan"),
        # Parsers disagree on which duplicate wins. The gateway must not check one name
        # while the downstream server runs the other, so it refuses the message.
        pytest.param(
            b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
            b' "params": {"name": "get_weather", "name": "admin_reset_key"}}',
            id="duplicate-key",
        ),
    ],
)
async def test_unparseable_bodies_get_a_parse_error(gateway, downstream, body):
    response = await gateway.post(GATEWAY_URL, content=body, headers=bearer("admin"))

    assert response.status_code == 400
    assert response.json() == _parse_error()
    assert downstream.requests == []


@pytest.mark.parametrize(
    ("message", "expected_id"),
    [
        pytest.param([rpc("tools/list")], None, id="batch"),
        pytest.param([], None, id="empty-batch"),
        pytest.param("tools/list", None, id="string"),
        pytest.param(42, None, id="number"),
        pytest.param(None, None, id="null"),
        pytest.param({"id": 3, "method": "tools/list"}, 3, id="no-jsonrpc-member"),
        pytest.param({"jsonrpc": "1.0", "id": 3, "method": "tools/list"}, 3, id="jsonrpc-1.0"),
        pytest.param({"jsonrpc": "2.0", "id": 3}, 3, id="no-method"),
        pytest.param({"jsonrpc": "2.0", "id": 3, "method": 5}, 3, id="method-not-string"),
        pytest.param(rpc("tools/list", id_=True), None, id="id-bool"),
        pytest.param(rpc("tools/list", id_=1.5), None, id="id-float"),
        pytest.param(rpc("tools/list", id_={"a": 1}), None, id="id-object"),
        pytest.param(rpc("tools/list", id_=None), None, id="id-null"),
        pytest.param({**rpc("tools/list", id_="abc"), "params": []}, "abc", id="params-array"),
        pytest.param(
            {"jsonrpc": "2.0", "id": 3, "result": {}, "error": {"code": 1, "message": "x"}},
            3,
            id="response-with-result-and-error",
        ),
        pytest.param({"jsonrpc": "2.0", "result": {}}, None, id="response-without-id"),
    ],
)
async def test_invalid_requests_are_rejected_before_downstream(
    gateway, downstream, message, expected_id
):
    # content=, not json=: httpx sends no body at all for json=None.
    body = json.dumps(message)
    response = await gateway.post(GATEWAY_URL, content=body, headers=bearer("admin"))

    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": expected_id,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert downstream.requests == []


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(None, id="no-params"),
        pytest.param({}, id="no-name"),
        pytest.param({"name": None}, id="name-null"),
        pytest.param({"name": 7}, id="name-number"),
        pytest.param({"name": ["admin_reset_key"]}, id="name-list"),
        pytest.param({"name": {"value": "admin_reset_key"}}, id="name-object"),
    ],
)
async def test_tool_calls_without_a_string_name_get_invalid_params(gateway, downstream, params):
    response = await gateway.post(
        GATEWAY_URL, json=rpc("tools/call", params, id_=9), headers=bearer("admin")
    )

    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 9,
        "error": {"code": -32602, "message": "Invalid params: params.name must be a string"},
    }
    assert downstream.requests == []


async def test_the_request_body_and_mcp_headers_reach_downstream_unchanged(gateway, downstream):
    body = b'{"jsonrpc":"2.0",  "id":"a-1", "method":"tools/list", "params":{"cursor":"x"}}'
    headers = {
        **bearer("viewer"),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": "session-abc",
        "Mcp-Protocol-Version": "2025-06-18",
        # MCP 2026-07-28 mirrors body fields into headers; the downstream server checks them.
        "Mcp-Method": "tools/list",
        "Mcp-Param-Region": "eu",
        "Cookie": "tracking=1",
        "X-Internal-Debug": "1",
    }

    await gateway.post(GATEWAY_URL, content=body, headers=headers)

    forwarded = downstream.requests[0]
    assert forwarded.method == "POST"
    assert forwarded.content == body
    assert forwarded.headers["content-type"] == "application/json"
    assert forwarded.headers["accept"] == "application/json, text/event-stream"
    assert forwarded.headers["mcp-session-id"] == "session-abc"
    assert forwarded.headers["mcp-protocol-version"] == "2025-06-18"
    assert forwarded.headers["mcp-method"] == "tools/list"
    assert forwarded.headers["mcp-param-region"] == "eu"
    # The gateway token is a credential for the gateway only.
    assert "authorization" not in forwarded.headers
    assert "cookie" not in forwarded.headers
    assert "x-internal-debug" not in forwarded.headers


@pytest.mark.parametrize(
    "downstream_reply",
    [
        pytest.param(httpx.Response(202), id="accepted-notification"),
        pytest.param(
            httpx.Response(
                200,
                content=b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
                headers={"content-type": "text/event-stream", "mcp-session-id": "s-9"},
            ),
            id="sse-stream",
        ),
        pytest.param(
            httpx.Response(
                404,
                json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "x"}},
                headers={"mcp-session-id": "s-9"},
            ),
            id="downstream-error-status",
        ),
    ],
)
async def test_the_downstream_reply_reaches_the_client_unchanged(
    gateway, downstream, downstream_reply
):
    downstream.reply = lambda _request: downstream_reply

    response = await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=bearer("viewer"))

    assert response.status_code == downstream_reply.status_code
    assert response.content == downstream_reply.content
    for name in ("content-type", "mcp-session-id"):
        assert response.headers.get(name) == downstream_reply.headers.get(name)


async def test_repeated_reply_headers_reach_the_client_as_separate_headers(gateway, downstream):
    downstream.reply = lambda _request: httpx.Response(
        200, json={}, headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")]
    )

    response = await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=bearer("viewer"))

    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]


async def test_repeated_accept_headers_are_combined_into_one(gateway, downstream):
    headers = [
        *bearer("viewer").items(),
        ("Accept", "application/json"),
        ("Accept", "text/event-stream"),
    ]

    await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=headers)

    # RFC 9110 section 5.3: repeated list headers are equal to one comma-joined header.
    assert downstream.requests[0].headers.get_list("accept") == [
        "application/json, text/event-stream"
    ]


def _raise(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def reply(request: httpx.Request) -> httpx.Response:
        raise error

    return reply


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (httpx.ConnectError("connection refused"), 502, "Downstream MCP server unavailable"),
        (httpx.ReadTimeout("timed out"), 504, "Downstream MCP server timed out"),
    ],
)
async def test_downstream_failures_become_a_clean_jsonrpc_error(
    gateway, downstream, error, status, message
):
    downstream.reply = _raise(error)

    response = await gateway.post(
        GATEWAY_URL, json=rpc("tools/list", id_="req-4"), headers=bearer("viewer")
    )

    assert response.status_code == status
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "req-4",
        "error": {"code": -32603, "message": message},
    }


@pytest.mark.parametrize(
    "gateway_settings",
    [GatewaySettings(downstream_url=DOWNSTREAM_URL, jwt_secret=SECRET, max_body_bytes=64)],
    ids=["limit-64-bytes"],
)
async def test_a_body_over_the_size_limit_is_rejected_before_downstream(gateway, downstream):
    body = json.dumps(rpc("tools/call", {"name": "get_weather", "arguments": {"x": "y" * 64}}))

    response = await gateway.post(GATEWAY_URL, content=body, headers=bearer("admin"))

    assert response.status_code == 413
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Request body too large"},
    }
    assert downstream.requests == []


async def test_each_decision_is_logged_without_the_token(gateway, caplog):
    caplog.set_level(logging.INFO, logger="mcp_gateway")
    token_headers = bearer("viewer")
    blocked = rpc("tools/call", {"name": "admin_reset_key"})

    await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=token_headers)
    await gateway.post(GATEWAY_URL, json=blocked, headers=token_headers)
    await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers={})

    records = [r.getMessage() for r in caplog.records]
    assert records == [
        # Caller-supplied strings are logged with repr(), so a newline cannot forge a log line.
        "allowed role=viewer method='tools/list' tool=None",
        "denied role=viewer method='tools/call' tool='admin_reset_key' code=-32001",
        "rejected unauthenticated request: missing or non-Bearer Authorization header",
    ]
    token = token_headers["Authorization"].removeprefix("Bearer ")
    assert all(token not in r for r in records)


@pytest.mark.parametrize(
    ("mirror_headers", "method", "params"),
    [
        pytest.param(
            {"Mcp-Method": "tools/call", "Mcp-Name": "admin_reset_key"},
            "tools/call",
            {"name": "get_weather"},
            id="name-header-differs",
        ),
        pytest.param(
            {"Mcp-Method": "tools/call", "Mcp-Name": "admin_reset_key"},
            "tools/list",
            None,
            id="method-header-differs",
        ),
        pytest.param(
            {"Mcp-Name": "admin_reset_key"},
            "tools/list",
            None,
            id="name-header-on-a-method-without-a-name",
        ),
    ],
)
async def test_mcp_mirror_headers_that_disagree_with_the_body_are_rejected(
    gateway, downstream, mirror_headers, method, params
):
    # The gateway authorizes from the body. A downstream server that routes by these headers
    # must never see a tool name that the gateway did not check.
    headers = {**bearer("viewer"), **mirror_headers}

    response = await gateway.post(GATEWAY_URL, json=rpc(method, params, id_=5), headers=headers)

    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {
            "code": -32600,
            "message": "Invalid Request: Mcp-* headers do not match the body",
        },
    }
    assert downstream.requests == []


async def test_a_repeated_mcp_header_is_rejected(gateway, downstream):
    # A check that reads the first value while the proxy forwards the last one is a bypass.
    headers = [
        *bearer("viewer").items(),
        ("Mcp-Name", "get_weather"),
        ("Mcp-Name", "admin_reset_key"),
    ]
    message = rpc("tools/call", {"name": "get_weather"}, id_=6)

    response = await gateway.post(GATEWAY_URL, json=message, headers=headers)

    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 6,
        "error": {"code": -32600, "message": "Invalid Request: repeated Mcp-Name header"},
    }
    assert downstream.requests == []


async def test_a_bad_tool_name_gets_invalid_params_even_with_an_mcp_name_header(
    gateway, downstream
):
    headers = {**bearer("admin"), "Mcp-Name": "7"}

    response = await gateway.post(GATEWAY_URL, json=rpc("tools/call", {"name": 7}), headers=headers)

    assert response.json()["error"]["code"] == -32602
    assert downstream.requests == []


async def test_mcp_mirror_headers_that_match_the_body_are_forwarded(gateway, downstream):
    headers = {**bearer("viewer"), "Mcp-Method": "tools/call", "Mcp-Name": "get_weather"}
    message = rpc("tools/call", {"name": "get_weather"})

    response = await gateway.post(GATEWAY_URL, json=message, headers=headers)

    assert response.status_code == 200
    assert len(downstream.requests) == 1


async def test_get_opens_the_downstream_event_stream(gateway, downstream):
    # GET /mcp is the stream for messages that the server starts itself.
    stream = (
        b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n'
    )
    downstream.reply = lambda _request: httpx.Response(
        200, content=stream, headers={"content-type": "text/event-stream"}
    )
    headers = {
        **bearer("viewer"),
        "Accept": "text/event-stream",
        "Mcp-Session-Id": "session-abc",
        "Last-Event-ID": "evt-7",
    }

    response = await gateway.get(GATEWAY_URL, headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == stream
    forwarded = downstream.requests[0]
    assert forwarded.method == "GET"
    assert str(forwarded.url) == DOWNSTREAM_URL
    assert forwarded.content == b""
    assert forwarded.headers["mcp-session-id"] == "session-abc"
    assert forwarded.headers["last-event-id"] == "evt-7"
    assert "authorization" not in forwarded.headers


async def test_delete_ends_the_downstream_session(gateway, downstream):
    downstream.reply = lambda _request: httpx.Response(200)
    headers = {**bearer("viewer"), "Mcp-Session-Id": "session-abc"}

    response = await gateway.delete(GATEWAY_URL, headers=headers)

    assert response.status_code == 200
    assert [(r.method, r.headers["mcp-session-id"]) for r in downstream.requests] == [
        ("DELETE", "session-abc")
    ]


@pytest.mark.parametrize("http_method", ["GET", "DELETE"])
async def test_get_and_delete_without_a_valid_token_are_rejected(gateway, downstream, http_method):
    response = await gateway.request(
        http_method, GATEWAY_URL, headers=bearer("viewer", expires_in=-60)
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == -32000
    assert downstream.requests == []


@pytest.mark.parametrize("http_method", ["PUT", "PATCH"])
async def test_other_http_methods_are_not_allowed(gateway, downstream, http_method):
    response = await gateway.request(http_method, GATEWAY_URL, headers=bearer("admin"))

    assert response.status_code == 405
    assert downstream.requests == []


async def test_the_event_stream_has_no_read_timeout_but_messages_do(gateway, downstream):
    # The GET stream may stay quiet for minutes; a read timeout would cut it. A POST still
    # needs one, so a hung downstream server cannot hold the request forever.
    downstream.reply = lambda _request: httpx.Response(200, json={})

    await gateway.get(GATEWAY_URL, headers=bearer("viewer"))
    await gateway.post(GATEWAY_URL, json=rpc("tools/list"), headers=bearer("viewer"))

    get_timeout, post_timeout = (r.extensions["timeout"] for r in downstream.requests)
    assert get_timeout["read"] is None
    assert get_timeout["connect"] is not None
    assert post_timeout["read"] is not None


async def test_a_downstream_failure_on_get_becomes_a_clean_jsonrpc_error(gateway, downstream):
    downstream.reply = _raise(httpx.ConnectError("connection refused"))

    response = await gateway.get(GATEWAY_URL, headers=bearer("viewer"))

    assert response.status_code == 502
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32603, "message": "Downstream MCP server unavailable"},
    }
