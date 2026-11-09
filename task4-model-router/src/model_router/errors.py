"""The one error payload the gateway sends. It never holds upstream text or internal details."""

from http import HTTPStatus

from starlette.responses import JSONResponse


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}

    def response(self, request_id: str) -> JSONResponse:
        body = {"error": {"code": self.code, "message": self.message, "request_id": request_id}}
        headers = {**self.headers, "x-request-id": request_id}
        return JSONResponse(body, status_code=self.status, headers=headers)


def unauthorized() -> GatewayError:
    return GatewayError(
        HTTPStatus.UNAUTHORIZED, "unauthorized", "A valid tenant API key is required",
        {"www-authenticate": "Bearer"},
    )  # fmt: skip


def invalid_request(message: str) -> GatewayError:
    return GatewayError(HTTPStatus.BAD_REQUEST, "invalid_request", message)


# Each error is a new object per raise: a shared exception object would collect the
# traceback and context of every request that raised it.
def internal_error() -> GatewayError:
    return GatewayError(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "internal_error",
        "The gateway failed to handle the request",
    )


def upstream_timeout() -> GatewayError:
    return GatewayError(
        HTTPStatus.GATEWAY_TIMEOUT, "upstream_timeout", "No model provider answered in time"
    )


def upstream_unavailable() -> GatewayError:
    return GatewayError(
        HTTPStatus.BAD_GATEWAY, "upstream_unavailable", "No model provider is available"
    )


def upstream_rejected() -> GatewayError:
    return invalid_request("The model provider rejected the request")


def request_too_large() -> GatewayError:
    return GatewayError(
        HTTPStatus.BAD_REQUEST,
        "request_too_large",
        "The request needs more tokens than the tenant's per-minute limit",
    )
