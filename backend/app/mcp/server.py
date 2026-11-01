"""MCP server: typed, read-only tools over the LedgerFlow backend services.

Mounted on the existing FastAPI app at ``/mcp`` (streamable HTTP) by
:mod:`app.main`. The server exposes **only** read-only source-data tools:
they call :class:`SourceDataService`, which parses the immutable
``data/northwind`` files and returns rows exactly as recorded.

Deliberately absent (validation-only accounting boundary, decision D10):
FX translation, elimination, NCI and statement-assembly tools. No stub,
no placeholder — the consolidation skill in Milestone 4 detects the
missing capability from the tool catalogue and ends the run ``blocked``
naming the dependency instead of fabricating results.

Error convention: service errors are re-raised as SDK ``ToolError`` with
our ``<code>: <message>`` prefix so the client can restore the typed
error class across the transport (see :mod:`app.mcp.client`).
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.core.errors import LedgerFlowError
from app.services.source_data import SourceDataService

SERVER_NAME = "ledgerflow"

SERVER_INSTRUCTIONS = (
    "LedgerFlow MCP server. All tools are READ-ONLY accessors over the "
    "immutable consolidation source dataset (entities, trial balances, FX "
    "rates, group chart of accounts, account mappings, intercompany "
    "positions, subledger controls, prior-year balances, monthly P&L, year-end "
    "adjustments). Tools return source rows exactly as recorded — they "
    "perform no financial calculations. Calculation services (FX "
    "translation, eliminations, NCI, statements) are intentionally not "
    "exposed here."
)


def _read_only_annotations(title: str) -> ToolAnnotations:
    """Annotations every tool carries: read-only, non-destructive, local."""
    return ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


def _invoke(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a backend service method, preserving typed errors over MCP."""
    try:
        return fn(*args, **kwargs)
    except LedgerFlowError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    except Exception as exc:  # unexpected defect — still typed for the client
        raise ToolError(f"service_error: {type(exc).__name__}: {exc}") from exc


def build_mcp_server(service: SourceDataService | None = None) -> MCPServer:
    """Create an :class:`MCPServer` with the source-data tool set registered.

    Args:
        service: Backend service the tools call. Defaults to one bound to
            process settings (``DATA_DIR``/northwind). Tests inject a
            service over a synthetic fixture directory.
    """
    svc = service if service is not None else SourceDataService()
    server = MCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool(annotations=_read_only_annotations("List source files"))
    def list_source_files() -> list[dict[str, Any]]:
        """List every file in the source dataset with its size in bytes."""
        return _invoke(svc.list_source_files)

    @server.tool(annotations=_read_only_annotations("List group entities"))
    def list_entities() -> list[dict[str, str]]:
        """List the group's legal entities (id, country, currency, TB file)."""
        return _invoke(svc.list_entities)

    @server.tool(annotations=_read_only_annotations("Get trial balance"))
    def get_trial_balance(entity: str) -> dict[str, Any]:
        """Get one entity's raw trial balance rows as recorded (US, UK or IN)."""
        return _invoke(svc.get_trial_balance, entity)

    @server.tool(annotations=_read_only_annotations("Get FX rates"))
    def get_fx_rates(
        base_currency: str | None = None, rate_type: str | None = None
    ) -> list[dict[str, str]]:
        """Get FX rate rows, optionally filtered by base currency / rate type."""
        return _invoke(svc.get_fx_rates, base_currency, rate_type)

    @server.tool(annotations=_read_only_annotations("Get group chart of accounts"))
    def get_group_coa() -> list[dict[str, str]]:
        """Get the group chart of accounts (code, type, statement mapping)."""
        return _invoke(svc.get_group_coa)

    @server.tool(annotations=_read_only_annotations("Get account mapping"))
    def get_account_mapping(entity: str) -> dict[str, Any]:
        """Get one entity's local-to-group account mapping rows (empty for US)."""
        return _invoke(svc.get_account_mapping, entity)

    @server.tool(annotations=_read_only_annotations("Get intercompany positions"))
    def get_intercompany() -> list[dict[str, str]]:
        """Get intercompany invoice positions across the group."""
        return _invoke(svc.get_intercompany)

    @server.tool(annotations=_read_only_annotations("Get subledger controls"))
    def get_subledger_controls(entity: str | None = None) -> list[dict[str, str]]:
        """Get subledger control totals, optionally filtered by entity id."""
        return _invoke(svc.get_subledger_controls, entity)

    @server.tool(annotations=_read_only_annotations("Get prior-year group balances"))
    def get_prior_year_group() -> list[dict[str, str]]:
        """Get prior-year group balances used for comparison."""
        return _invoke(svc.get_prior_year_group)

    @server.tool(annotations=_read_only_annotations("Get monthly P&L movements"))
    def get_monthly_pnl(entity: str | None = None) -> list[dict[str, str]]:
        """Get monthly P&L movements (IN ledger data), optionally by entity."""
        return _invoke(svc.get_monthly_pnl, entity)

    @server.tool(annotations=_read_only_annotations("Get year-end adjustments"))
    def get_adjustments() -> dict[str, Any]:
        """Get the year-end adjustment entries, verbatim from source."""
        return _invoke(svc.get_adjustments)

    return server
