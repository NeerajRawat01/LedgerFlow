"""MCP server tools over an in-memory transport (no sockets, no network).

The real ``data/northwind`` dataset is only ever *read* (hash-verified
before/after) in one smoke test; every functional test uses the synthetic
fixture from conftest.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from app.core.errors import NotFoundError, ServiceError
from app.mcp.server import build_mcp_server
from app.services.source_data import SourceDataService

EXPECTED_TOOLS = {
    "list_source_files",
    "list_entities",
    "get_trial_balance",
    "get_fx_rates",
    "get_group_coa",
    "get_account_mapping",
    "get_intercompany",
    "get_subledger_controls",
    "get_prior_year_group",
    "get_monthly_pnl",
    "get_adjustments",
}


@asynccontextmanager
async def server_session(server: MCPServer):
    """An initialized ClientSession over in-memory streams."""
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        task = asyncio.create_task(
            server._lowlevel_server.run(  # noqa: SLF001 - SDK-documented test API
                server_streams[0],
                server_streams[1],
                server._lowlevel_server.create_initialization_options(),  # noqa: SLF001
            )
        )
        try:
            async with ClientSession(
                client_streams[0], client_streams[1]
            ) as session:
                await session.initialize()
                yield session
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def payload(result):
    """Return a tool's structured-content payload.

    The SDK wraps a tool that returns a list as ``{"result": [...]}`` but
    returns a tool that returns a dict verbatim (no ``"result"`` key). Only
    unwrap the explicit wrapper — mirroring ``MCPClient._payload`` — so both
    shapes survive intact.
    """
    structured = result.structured_content
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


# --- tool catalogue ---------------------------------------------------------


async def test_catalog_is_fully_typed_and_read_only(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        result = await session.list_tools()

    tools = {t.name: t for t in result.tools}
    assert set(tools) == EXPECTED_TOOLS
    for tool in tools.values():
        assert tool.description, f"{tool.name} has no description"
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.input_schema.get("type") == "object", tool.name

    # entity is a required string for the trial-balance tool
    schema = tools["get_trial_balance"].input_schema
    assert "entity" in schema["required"]
    assert schema["properties"]["entity"]["type"] == "string"


# --- source-data tools ------------------------------------------------------


async def test_list_entities(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        result = await session.call_tool("list_entities", {})

    assert result.is_error is False
    entities = payload(result)
    assert [e["entity_id"] for e in entities] == ["US", "UK", "IN"]
    assert entities[1]["functional_currency"] == "GBP"


async def test_get_trial_balance_by_entity(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        us = payload(await session.call_tool("get_trial_balance", {"entity": "US"}))
        uk = payload(await session.call_tool("get_trial_balance", {"entity": "uk"}))

    assert us["source_file"] == "tb_us.csv"
    assert us["columns"] == ["account_code", "account_name", "debit", "credit"]
    assert us["rows"][0]["debit"] == "125000.00"
    # values stay raw strings — no parsing, no interpretation
    assert isinstance(us["rows"][0]["debit"], str)

    assert uk["source_file"] == "tb_uk.csv"  # case-insensitive entity id
    assert uk["rows"][1]["cost_centre"] == "MAN"


async def test_get_trial_balance_unknown_entity_is_typed_not_found(
    northwind_data_dir,
):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        result = await session.call_tool("get_trial_balance", {"entity": "FR"})

    assert result.is_error is True
    text = result.content[0].text
    assert "not_found" in text
    assert "FR" in text


async def test_account_mapping_us_is_empty_uk_is_mapped(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        us = payload(
            await session.call_tool("get_account_mapping", {"entity": "US"})
        )
        uk = payload(
            await session.call_tool("get_account_mapping", {"entity": "UK"})
        )

    # US posts directly to the group chart: no mapping file, no rows.
    assert us == {"entity": "US", "source_file": None, "rows": []}
    assert uk["source_file"] == "map_uk.csv"
    assert uk["rows"][0]["group_code"] == "4000"


async def test_fx_rate_filters(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        gbp = payload(
            await session.call_tool("get_fx_rates", {"base_currency": "GBP"})
        )
        average = payload(
            await session.call_tool("get_fx_rates", {"rate_type": "average"})
        )
        all_rates = payload(await session.call_tool("get_fx_rates", {}))

    assert len(all_rates) == 4
    assert {r["rate_type"] for r in gbp} == {"opening", "average"}
    assert len(average) == 1
    assert average[0]["rate"] == "1.2800"


async def test_monthly_pnl_entity_filter(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        all_rows = payload(await session.call_tool("get_monthly_pnl", {}))
        in_rows = payload(
            await session.call_tool("get_monthly_pnl", {"entity": "IN"})
        )
        us_rows = payload(
            await session.call_tool("get_monthly_pnl", {"entity": "US"})
        )

    assert len(all_rows) == 3
    assert len(in_rows) == 3  # dataset carries IN monthly data only
    assert us_rows == []  # factually empty, not an error


async def test_subledger_controls_filter_and_other_readers(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        uk_controls = payload(
            await session.call_tool("get_subledger_controls", {"entity": "UK"})
        )
        coa = payload(await session.call_tool("get_group_coa", {}))
        ic = payload(await session.call_tool("get_intercompany", {}))
        prior = payload(await session.call_tool("get_prior_year_group", {}))
        adjustments = payload(await session.call_tool("get_adjustments", {}))
        files = payload(await session.call_tool("list_source_files", {}))

    assert uk_controls == [
        {
            "entity_id": "UK",
            "ledger": "AR",
            "currency": "GBP",
            "control_total": "92000.00",
            "as_of": "2024-12-31",
        }
    ]
    assert coa[1]["account_type"] == "revenue"
    assert {row["doc_ref"] for row in ic} == {"INV-US-2291", "INV-UK-0042"}
    assert prior[0]["amount_gbp"] == "310000.00"
    assert adjustments["batch"] == "SYNTH-ADJ-2024"
    assert len(adjustments["entries"]) == 2
    assert "entities.csv" in {f["name"] for f in files}


# --- error behaviour --------------------------------------------------------


async def test_missing_argument_is_reported_as_tool_error(northwind_data_dir):
    server = build_mcp_server(SourceDataService(northwind_data_dir))
    async with server_session(server) as session:
        result = await session.call_tool("get_trial_balance", {})

    assert result.is_error is True
    assert "validation error" in result.content[0].text.lower()


async def test_missing_dataset_is_typed_not_found(tmp_path):
    server = build_mcp_server(SourceDataService(tmp_path / "empty"))
    async with server_session(server) as session:
        result = await session.call_tool("list_entities", {})

    assert result.is_error is True
    assert "not_found" in result.content[0].text


def test_service_raises_typed_errors_directly(tmp_path):
    service = SourceDataService(tmp_path / "nope")
    with pytest.raises(NotFoundError):
        service.list_entities()

    data = tmp_path / "data" / "northwind"
    data.mkdir(parents=True)
    (data / "entities.csv").write_text(
        "entity_id,tb_file\nUS,tb_us.csv\n", encoding="utf-8"
    )
    (data / "adjustments.json").write_text("{not json", encoding="utf-8")
    service2 = SourceDataService(tmp_path / "data")

    # Malformed JSON is a typed ServiceError, never a crash.
    with pytest.raises(ServiceError) as excinfo:
        service2.get_adjustments()
    assert excinfo.value.code == "service_error"

    # An entity recorded without a TB file is a typed not_found.
    (data / "entities.csv").write_text(
        "entity_id,tb_file\nXX,\n", encoding="utf-8"
    )
    with pytest.raises(NotFoundError):
        service2.get_trial_balance("XX")


# --- real dataset (read-only smoke) ----------------------------------------


def _sha256_tree(root: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.iterdir())
        if p.is_file()
    }


REPO_DATA = Path(__file__).resolve().parents[2] / "data"


async def test_real_northwind_data_is_readable_and_untouched():
    """Smoke: tools parse the real dataset; reading never modifies it."""
    northwind = REPO_DATA / "northwind"
    if not northwind.is_dir():
        pytest.skip("real dataset not present")

    before = _sha256_tree(northwind)
    service = SourceDataService(REPO_DATA)

    entities = service.list_entities()
    assert [e["entity_id"] for e in entities] == ["US", "UK", "IN"]

    us = service.get_trial_balance("US")
    assert us["source_file"] == "tb_us.csv"
    assert len(us["rows"]) > 0

    uk = service.get_trial_balance("UK")
    assert len(uk["rows"]) > 0

    assert len(service.get_fx_rates()) > 0
    assert len(service.get_group_coa()) > 0
    assert service.get_account_mapping("US")["rows"] == []
    assert len(service.get_adjustments()["entries"]) > 0

    after = _sha256_tree(northwind)
    assert after == before, "read-only violation: dataset files changed"
