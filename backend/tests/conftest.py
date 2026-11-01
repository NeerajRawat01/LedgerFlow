"""Shared fixtures for the agentic (Phase 2) test suite.

Every test gets an isolated runtime: temp storage directories, a registry
with stub skills, and a fake LLM. No network access ever happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agents.graph import AgentRuntime, build_graph
from app.agents.runner import AgentRunner
from app.agents.skills.base import Skill, SkillContext, SkillSpec
from app.agents.skills.registry import SkillRegistry
from app.agents.state import AgentMode, RunStatus, SkillResult
from app.agents.thread_service import ThreadService
from app.core.errors import LLMNotConfiguredError


class StubConsolidationSkill(Skill):
    """Stands in for the real Consolidation skill (Milestone 4)."""

    spec = SkillSpec(
        name="consolidation",
        purpose="Run a multi-entity consolidation for a period.",
        instructions="Validate inputs, then orchestrate the consolidation.",
        execution_procedure=("load", "validate", "translate", "eliminate"),
        permitted_tools=("get_trial_balance", "get_trial_balances"),
        validation_requirements=("trial balance must balance",),
        output_expectations=("consolidated balances", "status"),
        allowed_modes=frozenset({AgentMode.CONSOLIDATION}),
        description="Consolidates subsidiary ledgers.",
    )

    def __init__(self, result: SkillResult | None = None) -> None:
        self.result = result

    async def execute(self, context: SkillContext) -> SkillResult:
        if self.result is not None:
            return self.result
        return SkillResult(
            status=RunStatus.BLOCKED,
            summary="blocked: fx_translation service not available",
            data={"period": context.intent.period},
            issues=[{"severity": "error", "code": "missing_dependency"}],
        )


class StubAnalysisSkill(Skill):
    """Stands in for the real Financial Analysis skill (Milestone 4)."""

    spec = SkillSpec(
        name="financial_analysis",
        purpose="Answer financial questions with evidence.",
        instructions="Gather evidence via tools, then answer.",
        execution_procedure=("interpret", "gather", "answer"),
        permitted_tools=("get_trial_balance", "get_kpis"),
        allowed_modes=frozenset({AgentMode.COPILOT}),
        description="Evidence-grounded financial analysis.",
    )

    def __init__(self, result: SkillResult | None = None) -> None:
        self.result = result

    async def execute(self, context: SkillContext) -> SkillResult:
        if self.result is not None:
            return self.result
        return SkillResult(
            status=RunStatus.COMPLETED,
            summary="analysis complete",
            answer="Revenue grew 12%, driven by the US entity.",
        )


def fake_llm(*responses: str) -> Callable[[], BaseChatModel]:
    """Provider returning a scripted fake LLM (a fresh one per call)."""
    messages = [AIMessage(content=r) for r in responses] or [
        AIMessage(content="{}")
    ]

    def provider() -> BaseChatModel:
        return FakeMessagesListChatModel(responses=list(messages))

    return provider


def unconfigured_llm() -> BaseChatModel:
    """Provider behaving like an environment without an API key."""
    raise LLMNotConfiguredError("GEMINI_API_KEY is not set")


@pytest.fixture
def make_runtime(tmp_path: Path):
    """Factory for isolated AgentRuntime instances."""
    counter = {"n": 0}

    def _make(
        registry: SkillRegistry | None = None,
        llm_provider: Callable[[], BaseChatModel] | None = None,
        settings: Any = None,
        tool_gateway: Any = None,
    ) -> AgentRuntime:
        counter["n"] += 1
        root = tmp_path / f"rt{counter['n']}"
        return AgentRuntime(
            registry=registry
            if registry is not None
            else SkillRegistry([StubConsolidationSkill(), StubAnalysisSkill()]),
            llm_provider=llm_provider or fake_llm(
                '{"kind": "financial_analysis", "topic": "revenue", '
                '"period": "2024", "entities": ["US"], '
                '"skill_hint": "financial_analysis", "requires_tools": true, '
                '"confidence": 0.9, "user_goal": "explain revenue"}'
            ),
            thread_service=ThreadService(root / "threads"),
            traces_dir=root / "traces",
            tool_gateway=tool_gateway,
            **({"settings": settings} if settings is not None else {}),
        )

    return _make


@pytest.fixture
def make_runner(make_runtime):
    """Factory for AgentRunner instances bound to fresh runtimes."""

    def _make(
        registry: SkillRegistry | None = None,
        llm_provider: Callable[[], BaseChatModel] | None = None,
        settings: Any = None,
        tool_gateway: Any = None,
    ) -> tuple[AgentRunner, AgentRuntime]:
        runtime = make_runtime(registry, llm_provider, settings, tool_gateway)
        return AgentRunner(runtime, build_graph(runtime)), runtime

    return _make


# --- synthetic source dataset (Milestone 3) --------------------------------


def write_northwind_fixture(root: Path) -> Path:
    """Write a minimal, schema-faithful Northwind dataset under ``root``.

    Mirrors the real ``data/northwind`` column layouts (including the US
    having no mapping file) but uses obviously synthetic values, so MCP
    tests never depend on — or touch — the immutable real dataset.
    """
    data = root / "northwind"
    data.mkdir(parents=True, exist_ok=True)

    def csv(name: str, header: str, rows: list[str]) -> None:
        (data / name).write_text(
            header + "\n" + "\n".join(rows) + "\n", encoding="utf-8"
        )

    csv(
        "entities.csv",
        "entity_id,legal_name,country,functional_currency,ownership_pct,"
        "acquisition_date,financial_year_end,erp_system,tb_file",
        [
            "US,Northwind Inc.,United States,USD,,,31-Dec,NetSuite,tb_us.csv",
            "UK,Northwind UK Ltd,United Kingdom,GBP,100,2016-03-01,31-Dec,"
            "Sage 50,tb_uk.csv",
            "IN,Northwind India Pvt Ltd,India,INR,80,2024-07-01,31-Mar,"
            "Tally Prime,tb_in.csv",
        ],
    )
    csv(
        "tb_us.csv",
        "account_code,account_name,debit,credit",
        [
            "1000,Cash,125000.00,",
            "4000,Revenue,,875000.00",
        ],
    )
    csv(
        "tb_uk.csv",
        "nominal_code,description,cost_centre,txn_currency,txn_amount,balance",
        [
            "7000,Sales,LON,GBP,42000.00,-42000.00",
            "7000,Sales,MAN,GBP,18000.00,-18000.00",
        ],
    )
    csv(
        "tb_in.csv",
        "ledger_code,ledger_name,period,amount,dr_cr",
        [
            "IN-CASH,Cash,2025-03,1500000.00,Dr",
            "IN-REV,Revenue,2025-03,9900000.00,Cr",
        ],
    )
    csv(
        "fx_rates.csv",
        "base_currency,quote_currency,rate_type,rate,as_of,convention",
        [
            "USD,GBP,closing,0.7910,2024-12-31,multiply",
            "GBP,USD,opening,1.2700,2024-01-01,multiply",
            "GBP,USD,average,1.2800,2024-12-31,multiply",
            "INR,USD,closing,0.0120,2024-12-31,multiply",
        ],
    )
    csv(
        "group_coa.csv",
        "account_code,account_name,account_type,statement,cash_flow_category,"
        "normal_balance",
        [
            "1000,Cash,asset,Balance Sheet,operating,debit",
            "4000,Revenue,revenue,Income Statement,operating,credit",
        ],
    )
    # US has NO mapping file (posts directly to the group chart) — mirrors
    # the real dataset.
    csv(
        "map_uk.csv",
        "local_code,local_name,group_code",
        ["7000,Sales,4000", "1200,Bank,1000"],
    )
    csv(
        "map_in.csv",
        "local_code,local_name,group_code",
        ["IN-CASH,Cash,1000", "IN-REV,Revenue,4000"],
    )
    csv(
        "intercompany.csv",
        "reporting_entity,counterparty,doc_ref,doc_date,doc_currency,"
        "doc_amount,booked_currency,booked_amount,description",
        [
            "US,UK,INV-US-2291,2024-11-15,USD,50000.00,USD,50000.00,"
            "Consulting fees",
            "UK,US,INV-UK-0042,2024-12-01,GBP,10000.00,GBP,10000.00,"
            "Management recharge",
        ],
    )
    csv(
        "subledger_controls.csv",
        "entity_id,ledger,currency,control_total,as_of",
        [
            "US,AR,USD,310000.00,2024-12-31",
            "UK,AR,GBP,92000.00,2024-12-31",
            "IN,AR,INR,2450000.00,2024-12-31",
        ],
    )
    csv(
        "prior_year_group.csv",
        "group_account,account_name,amount_gbp,as_of,note",
        [
            "1000,Cash,310000.00,2023-12-31,audited",
            "4000,Revenue,1420000.00,2023-12-31,audited",
        ],
    )
    csv(
        "in_pl_monthly.csv",
        "ledger_code,ledger_name,month,amount,dr_cr",
        [
            "IN-REV,Revenue,2024-10,780000.00,Cr",
            "IN-REV,Revenue,2024-11,810000.00,Cr",
            "IN-COGS,COGS,2024-11,460000.00,Dr",
        ],
    )
    (data / "adjustments.json").write_text(
        '{"batch": "SYNTH-ADJ-2024", "period": "FY2024", '
        '"note": "synthetic fixture", "entries": ['
        '{"id": "SYN-001", "account": "4000", "amount": 1000, '
        '"description": "accrual"},'
        '{"id": "SYN-002", "account": "1000", "amount": -1000, '
        '"description": "accrual offset"}]}',
        encoding="utf-8",
    )
    return data


@pytest.fixture
def northwind_data_dir(tmp_path: Path) -> Path:
    """A DATA_DIR containing a synthetic ``northwind`` dataset (MCP tests)."""
    root = tmp_path / "data"
    write_northwind_fixture(root)
    return root
