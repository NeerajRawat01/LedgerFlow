"""LedgerFlow FastAPI application entrypoint.

``create_app`` builds the application: Phase 1 routers plus the read-only
MCP tool server mounted at ``/mcp`` (streamable HTTP). Starlette does NOT
propagate lifespan events into mounted sub-apps, so the MCP session
manager is started from this app's own lifespan.

``uvicorn app.main:app`` keeps working: the module-level ``app`` is built
with the process-default MCP server (tools over ``DATA_DIR``/northwind).
Tests inject a fixture-backed MCP server through ``create_app(mcp=...)``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.runner import AgentRunner
from app.api import consolidations, health, root
from app.core.config import settings
from app.mcp.server import build_mcp_server
from app.services.consolidation_runs import ConsolidationRunStore
from app.storage.filesystem import storage_paths
from mcp.server.mcpserver import MCPServer

# A factory returning a fresh AgentRunner per run. Kept as a callable (not a
# shared instance) so tests can inject a runner over an in-memory gateway.
RunnerFactory = Callable[[], AgentRunner]


def _default_runner_factory() -> AgentRunner:
    """The production runner: real runtime, loopback MCP gateway, default skills."""
    from app.agents.graph import default_runtime
    from app.agents.skills.registry import build_default_registry

    return AgentRunner(default_runtime(build_default_registry()))


def create_app(
    mcp: MCPServer | None = None,
    runner_factory: RunnerFactory | None = None,
) -> FastAPI:
    """Build the LedgerFlow application.

    Args:
        mcp: An already-built MCP server. Defaults to one whose tools read
            the process source dataset (``DATA_DIR``/northwind).
        runner_factory: Builds the ``AgentRunner`` a consolidation run uses.
            Defaults to the production runner over ``default_runtime``;
            tests inject one backed by an in-memory MCP gateway.
    """
    mcp_server = mcp if mcp is not None else build_mcp_server()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """Start filesystem layout and the MCP session manager."""
        storage_paths.ensure_layout()
        async with mcp_server.session_manager.run():
            yield

    application = FastAPI(
        title="LedgerFlow API",
        description=(
            "AI-powered financial consolidation and reporting platform for "
            "multi-entity, multi-currency groups. Phase 2 exposes the shared "
            "agent infrastructure, a read-only MCP tool server at /mcp, and "
            "consolidation run endpoints that drive the shared agent."
        ),
        version=settings.version,
        lifespan=lifespan,
    )

    # Execution seams for the consolidation router. Stored on app.state so
    # they are resolved per request and can be overridden in tests without
    # touching the router.
    application.state.runner_factory = runner_factory or _default_runner_factory
    application.state.run_store = ConsolidationRunStore(storage_paths.runs_dir)
    application.state.traces_dir = storage_paths.traces_dir

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(root.router)
    application.include_router(health.router)
    application.include_router(consolidations.router)

    # Mounted last so every application route above keeps priority; the
    # sub-app only ever sees paths under /mcp.
    application.mount(
        "/", mcp_server.streamable_http_app(streamable_http_path="/mcp")
    )
    return application


app = create_app()
