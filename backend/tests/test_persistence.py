"""Filesystem persistence tests (Milestone 2).

Covers the storage primitives (atomic writes, JSONL traces), conversation
thread persistence with isolation between threads, and secret redaction so
API keys can never be written to disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.thread_service import (
    ThreadService,
    deserialize_message,
    serialize_message,
)
from app.agents.tracing import TraceRecorder, redact
from app.core.errors import NotFoundError, StorageError
from app.storage.filesystem import (
    FileStoragePaths,
    append_jsonl,
    atomic_write_json,
    iter_jsonl,
    read_json,
)


# --- storage primitives -----------------------------------------------------


def test_atomic_write_then_read(tmp_path: Path):
    target = tmp_path / "nested" / "file.json"
    atomic_write_json(target, {"a": 1, "b": [1, 2]})

    assert read_json(target) == {"a": 1, "b": [1, 2]}
    # no temp files left behind
    leftovers = [p.name for p in target.parent.iterdir() if p.name != "file.json"]
    assert leftovers == []


def test_read_missing_json_raises_storage_error(tmp_path: Path):
    with pytest.raises(StorageError) as exc:
        read_json(tmp_path / "absent.json")
    assert exc.value.code == "storage_error"


def test_read_corrupt_json_raises_storage_error(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(StorageError):
        read_json(bad)


def test_append_jsonl_and_iter(tmp_path: Path):
    target = tmp_path / "events.jsonl"
    for i in range(3):
        append_jsonl(target, {"i": i})
    records = list(iter_jsonl(target))
    assert [r["i"] for r in records] == [0, 1, 2]


def test_iter_jsonl_skips_malformed_lines(tmp_path: Path):
    target = tmp_path / "partial.jsonl"
    target.write_text('{"ok": 1}\nGARBAGE LINE\n{"ok": 2}\n', encoding="utf-8")
    records = list(iter_jsonl(target))
    assert [r["ok"] for r in records] == [1, 2]


def test_iter_missing_jsonl_raises(tmp_path: Path):
    with pytest.raises(StorageError):
        list(iter_jsonl(tmp_path / "nope.jsonl"))


def test_ensure_layout_creates_writable_dirs(tmp_path: Path):
    paths = FileStoragePaths(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        output_dir=tmp_path / "output",
    )
    paths.ensure_layout()
    for d in (
        paths.storage_dir,
        paths.runs_dir,
        paths.issues_dir,
        paths.agent_logs_dir,
        paths.threads_dir,
        paths.traces_dir,
        paths.output_dir,
    ):
        assert d.is_dir()
        assert os.access(d, os.W_OK)
    # source data dir is never created by ensure_layout
    assert not paths.source_data_dir.exists()


# --- message round-trip -----------------------------------------------------


def test_message_serialization_round_trip():
    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="hi there", additional_kwargs={"citation": "x"}),
        SystemMessage(content="be helpful"),
    ]
    for original in messages:
        record = serialize_message(original)
        # JSON-safe
        json.dumps(record)
        restored = deserialize_message(record)
        assert restored.type == original.type
        assert restored.content == original.content


def test_tool_message_round_trip():
    from langchain_core.messages import ToolMessage

    original = ToolMessage(
        content="42", name="get_kpis", tool_call_id="call_1"
    )
    restored = deserialize_message(serialize_message(original))
    assert isinstance(restored, ToolMessage)
    assert restored.tool_call_id == "call_1"
    assert restored.name == "get_kpis"


def test_deserialize_unknown_type_defaults_to_human():
    restored = deserialize_message({"type": "weird", "content": "x"})
    assert isinstance(restored, HumanMessage)


# --- thread service ---------------------------------------------------------


def test_thread_save_and_load_round_trip(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    messages = [HumanMessage(content="q"), AIMessage(content="a")]

    service.save("thread_1", messages, mode="copilot")
    loaded = service.load("thread_1")

    assert [m.content for m in loaded] == ["q", "a"]
    assert loaded[0].type == "human"
    assert loaded[1].type == "ai"


def test_load_missing_thread_returns_empty(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    assert service.load("never_seen") == []
    assert not service.exists("never_seen")


def test_thread_isolation_two_files(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    service.save("alpha", [HumanMessage(content="alpha-only")])
    service.save("beta", [HumanMessage(content="beta-only")])

    assert [m.content for m in service.load("alpha")] == ["alpha-only"]
    assert [m.content for m in service.load("beta")] == ["beta-only"]
    # two separate files on disk
    files = sorted(p.name for p in (tmp_path / "threads").glob("*.json"))
    assert files == ["alpha.json", "beta.json"]


def test_thread_save_is_idempotent_and_preserves_created_at(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    service.save("t", [HumanMessage(content="1")])
    first = service.meta("t")
    service.save("t", [HumanMessage(content="1"), AIMessage(content="2")])
    second = service.meta("t")

    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert [m.content for m in service.load("t")] == ["1", "2"]


def test_thread_persists_across_service_restart(tmp_path: Path):
    # simulate a backend restart: a brand-new service object reads the same dir
    ThreadService(tmp_path / "threads").save(
        "durable", [HumanMessage(content="persisted")]
    )

    restarted = ThreadService(tmp_path / "threads")
    assert [m.content for m in restarted.load("durable")] == ["persisted"]


def test_thread_meta_and_list(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    service.save("a", [HumanMessage(content="x")], mode="copilot")
    service.save("b", [], mode="consolidation")

    meta = service.meta("a")
    assert meta["mode"] == "copilot"
    assert "messages" not in meta

    listed = service.list()
    assert {t["thread_id"] for t in listed} == {"a", "b"}
    assert all("messages" not in t for t in listed)


def test_meta_missing_thread_raises(tmp_path: Path):
    with pytest.raises(NotFoundError) as exc:
        ThreadService(tmp_path / "threads").meta("ghost")
    assert exc.value.code == "not_found"


def test_delete_thread(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    service.save("gone", [HumanMessage(content="x")])
    service.delete("gone")
    assert not service.exists("gone")
    with pytest.raises(NotFoundError):
        service.delete("gone")


@pytest.mark.parametrize("bad_id", ["", "../evil", "a/b", "a\\b", ".."])
def test_invalid_thread_id_rejected(tmp_path: Path, bad_id: str):
    service = ThreadService(tmp_path / "threads")
    with pytest.raises(StorageError):
        service.save(bad_id, [])
    with pytest.raises(StorageError):
        service.load(bad_id)


def test_thread_save_atomic_no_temp_files(tmp_path: Path):
    service = ThreadService(tmp_path / "threads")
    service.save("t", [HumanMessage(content="x")])
    leftovers = [p.name for p in (tmp_path / "threads").iterdir()]
    assert leftovers == ["t.json"]


# --- trace recorder ---------------------------------------------------------


def test_trace_events_written_and_read_back(tmp_path: Path):
    trace = TraceRecorder(tmp_path, "trace_1")
    trace.event("a", {"x": 1})
    trace.event("b", {"y": 2})

    events = trace.read_events()
    assert [e["event"] for e in events] == ["a", "b"]
    assert [e["seq"] for e in events] == [1, 2]
    assert all(e["trace_id"] == "trace_1" for e in events)
    assert events[0]["ts"]


def test_trace_seq_resumes_across_recorder_instances(tmp_path: Path):
    # The graph creates a fresh recorder per node; numbering must continue.
    TraceRecorder(tmp_path, "t").event("first")
    TraceRecorder(tmp_path, "t").event("second")

    events = TraceRecorder(tmp_path, "t").read_events()
    assert [e["event"] for e in events] == ["first", "second"]
    assert [e["seq"] for e in events] == [1, 2]


def test_trace_isolated_per_trace_id(tmp_path: Path):
    TraceRecorder(tmp_path, "t1").event("one")
    TraceRecorder(tmp_path, "t2").event("two")

    assert [e["event"] for e in TraceRecorder(tmp_path, "t1").read_events()] == [
        "one"
    ]
    assert [e["event"] for e in TraceRecorder(tmp_path, "t2").read_events()] == [
        "two"
    ]


def test_read_events_for_unwritten_trace_returns_empty(tmp_path: Path):
    assert TraceRecorder(tmp_path, "never").read_events() == []


def test_trace_payload_is_json_safe(tmp_path: Path):
    trace = TraceRecorder(tmp_path, "t_json")
    trace.event("data", {"set_like": [1, 2], "nested": {"a": [3]}})
    raw = (tmp_path / "t_json.jsonl").read_text(encoding="utf-8")
    json.loads(raw)  # must parse line by line
    for line in raw.strip().splitlines():
        json.loads(line)


# --- redaction --------------------------------------------------------------


def test_redact_masks_google_api_key_pattern():
    key = "AIzaSy" + "A" * 33
    payload = {"note": f"using key {key} now"}
    out = redact(payload)
    assert key not in json.dumps(out)
    assert "***REDACTED***" in out["note"]


def test_redact_masks_sensitive_keys():
    out = redact(
        {
            "api_key": "whatever",
            "Authorization": "Bearer xyz",
            "token": "abc",
            "safe": "visible",
        }
    )
    assert out["api_key"] == "***REDACTED***"
    assert out["Authorization"] == "***REDACTED***"
    assert out["token"] == "***REDACTED***"
    assert out["safe"] == "visible"


def test_redact_replaces_known_secret_values(tmp_path: Path):
    secret = "my-super-secret-key-value"
    out = redact({"msg": f"connect with {secret}"}, extra_secrets=[secret])
    assert secret not in out["msg"]


def test_redact_short_secrets_ignored_and_non_strings_untouched():
    # short strings are not treated as secrets (avoid mangling normal text)
    out = redact("order 12345 settled", extra_secrets=["12345"])
    assert out == "order 12345 settled"
    assert redact(42) == 42
    assert redact(None) is None
    assert redact([1, "plain"]) == [1, "plain"]


def test_trace_writes_redacted_payload(tmp_path: Path):
    trace = TraceRecorder(tmp_path, "t_secret")
    secret_key = "AIzaSy" + "B" * 33
    trace.event(
        "llm_call",
        {"prompt": f"key={secret_key}"},
        secrets=[secret_key],
    )
    raw = (tmp_path / "t_secret.jsonl").read_text(encoding="utf-8")
    assert secret_key not in raw


def test_nested_redaction_in_lists_and_dicts():
    out = redact(
        {
            "steps": [
                {"google_api_key": "AIza" + "1" * 35},
                {"detail": "AIza" + "2" * 35},
            ]
        }
    )
    assert json.dumps(out).count("***REDACTED***") == 2
