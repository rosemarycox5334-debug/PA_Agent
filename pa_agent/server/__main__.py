"""服务端入口：python -m pa_agent.server"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from pa_agent.server.api import create_app
    from pa_agent.server.bootstrap import bootstrap_headless

    ctx = bootstrap_headless()
    app = create_app(ctx)
    host = os.environ.get("PA_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("PA_SERVER_PORT", "8688"))
    ctx.logger.info("PA Agent 服务端启动：http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
