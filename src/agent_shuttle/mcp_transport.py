"""stdio and authenticated, host-local Streamable HTTP transports."""

import argparse
import hmac
import os

from starlette.responses import JSONResponse

from .presentation import VERSION
from .runtime_context import require_coordinator

TOKEN_ENV = "AGENT_SHUTTLE_MCP_TOKEN"


class LocalMcpAuth:
    def __init__(self, app, token: str, port: int):
        if len(token) < 32:
            raise ValueError("MCP token must contain at least 32 characters")
        self.app, self.token = app, token.encode("utf-8")
        self.host = f"127.0.0.1:{port}".encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        values = lambda key: [v for k, v in headers if k.lower() == key]
        auth = values(b"authorization")
        status = None
        if values(b"host") != [self.host]:
            status = 421
        elif values(b"origin"):
            status = 403
        elif len(auth) != 1 or not hmac.compare_digest(auth[0], b"Bearer " + self.token):
            status = 401
        if status:
            return await JSONResponse({"error": "MCP access denied"}, status_code=status)(scope, receive, send)
        if scope["path"] == "/health" and scope["method"] == "GET":
            return await JSONResponse({"name": "Agent Shuttle", "version": VERSION})(scope, receive, send)
        await self.app(scope, receive, send)


def run_server(mcp, argv=None):
    parser = argparse.ArgumentParser(prog="agent-shuttle-mcp")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args(argv)
    # Remove the token from the environment before any harness can inherit it.
    token = os.environ.pop(TOKEN_ENV, None)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return
    require_coordinator()
    if not token or len(token) < 32:
        parser.error(f"HTTP requires {TOKEN_ENV} with at least 32 characters")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = args.port
    mcp.settings.stateless_http = True
    app = LocalMcpAuth(mcp.streamable_http_app(), token, args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
