"""Issue a development JWT: `uv run issue-token --role admin`.

Real deployments get tokens from an identity provider; this command only exists so the
gateway can be run and tried locally. It signs with the same GATEWAY_JWT_SECRET.
"""

import argparse
import sys
import time

import jwt

from mcp_gateway.auth import JWT_ALGORITHM, Role
from mcp_gateway.config import ConfigurationError, GatewaySettings

DEFAULT_TTL_SECONDS = 3600


def issue_token(role: Role, secret: str, ttl_seconds: int, subject: str) -> str:
    claims = {"sub": subject, "role": role.value, "exp": int(time.time()) + ttl_seconds}
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", required=True, choices=[role.value for role in Role])
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="lifetime, seconds")
    parser.add_argument("--sub", default="local-dev-user", help="subject claim")
    args = parser.parse_args(argv)
    try:
        secret = GatewaySettings.from_env().jwt_secret
    except ConfigurationError as exc:
        sys.exit(f"issue-token: {exc}")
    sys.stdout.write(issue_token(Role(args.role), secret, args.ttl, args.sub) + "\n")


if __name__ == "__main__":
    main()
