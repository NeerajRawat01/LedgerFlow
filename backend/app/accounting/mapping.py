"""Account mapping: local chart of accounts → group chart of accounts.

Each entity's trial balance records local accounts that must be resolved to
the group chart of accounts before consolidation. The mapping schemas
discovered in the source dataset are:

* ``map_uk.csv`` / ``map_in.csv`` — ``local_code, local_name, group_code``.
  The key is ``local_code`` (verified unique in both files; no additional
  dimension is required).
* **US** has no mapping file: its trial balance already posts to the group
  chart (every US ``account_code`` is a group ``account_code``), so the US
  mapping is the **identity** mapping.

Design rules honoured here:

* mappings are loaded through :class:`SourceDataService` (no duplicated CSV
  readers);
* many-to-one mappings (several local accounts → one group account) are
  **valid aggregation**, never flagged as errors;
* ambiguous/conflicting mappings are **reported, never auto-resolved**;
* no FX conversion happens in this stage — original debit/credit and the
  functional currency are carried through unchanged;
* every mapped row keeps full source traceability.
"""

from __future__ import annotations

from collections import defaultdict

from app.accounting.models import (
    GroupAccount,
    MappedRow,
    MappedTrialBalance,
    MappingEntry,
    Severity,
    TrialBalance,
    ValidationIssue,
)
from app.services.source_data import SourceDataService

# Entities whose local accounts are resolved through an explicit mapping file.
# US is deliberately absent: it maps directly (identity).
_MAPPED_ENTITIES = {"UK", "IN"}


class AccountMapper:
    """Resolve local trial-balance accounts to the group chart of accounts."""

    def __init__(self, service: SourceDataService | None = None) -> None:
        self.service = service if service is not None else SourceDataService()
        self._coa: dict[str, GroupAccount] | None = None

    # -- loading -----------------------------------------------------------

    @property
    def group_coa(self) -> dict[str, GroupAccount]:
        """The group chart of accounts, keyed by account code."""
        if self._coa is None:
            self._coa = {
                row["account_code"].strip(): GroupAccount(
                    account_code=row["account_code"].strip(),
                    account_name=row.get("account_name", "").strip(),
                    account_type=row.get("account_type", "").strip(),
                    statement=row.get("statement", "").strip(),
                    cash_flow_category=row.get("cash_flow_category", "").strip(),
                    normal_balance=row.get("normal_balance", "").strip(),
                )
                for row in self.service.get_group_coa()
            }
        return self._coa

    def load_mapping(self, entity: str) -> tuple[list[MappingEntry], str | None]:
        """Load one entity's mapping records plus their source file name.

        For US (no mapping file) this returns ``([], None)`` — the caller uses
        the identity mapping instead. Records are returned verbatim (including
        incomplete ones) so the validator can report them; this method does
        not silently drop or repair anything.
        """
        entity_id = (entity or "").strip().upper()
        source = self.service.get_account_mapping(entity_id)
        source_file = source.get("source_file")
        entries: list[MappingEntry] = []
        for offset, row in enumerate(source.get("rows") or []):
            entries.append(
                MappingEntry(
                    entity=entity_id,
                    source_file=source_file,
                    source_row=offset + 1,
                    local_code=(row.get("local_code") or "").strip(),
                    local_name=(row.get("local_name") or "").strip() or None,
                    group_code=(row.get("group_code") or "").strip(),
                    raw={k: (v or "") for k, v in row.items()},
                )
            )
        return entries, source_file

    # -- validation --------------------------------------------------------

    def validate_mapping(
        self, entity: str, entries: list[MappingEntry]
    ) -> list[ValidationIssue]:
        """Validate mapping records; return structured issues (no repair).

        Detects:
        * invalid/incomplete records (missing local or group code),
        * duplicate mapping records (same local code recorded twice),
        * conflicting mappings (same local code → more than one group code),
        * group codes that do not exist in the group chart of accounts,
        * a group account whose name matches the local name but whose mapped
          group account differs (a probable mis-mapping, e.g. UK 2310).
        """
        issues: list[ValidationIssue] = []
        by_local: dict[str, list[MappingEntry]] = defaultdict(list)

        for entry in entries:
            if not entry.local_code or not entry.group_code:
                issues.append(
                    ValidationIssue(
                        code="invalid_mapping_record",
                        severity=Severity.ERROR,
                        message=(
                            f"{entity} mapping row {entry.source_row}: missing "
                            f"local_code or group_code"
                        ),
                        entity=entity,
                        source_file=entry.source_file,
                        source_row=entry.source_row,
                        account_code=entry.local_code or None,
                        details={"raw": entry.raw},
                    )
                )
                continue
            by_local[entry.local_code].append(entry)

        for local_code, records in sorted(by_local.items()):
            group_codes = {r.group_code for r in records}

            # Duplicate mapping records: the same local code recorded more than
            # once at all.
            if len(records) > 1:
                issues.append(
                    ValidationIssue(
                        code="duplicate_mapping_record",
                        severity=Severity.WARNING,
                        message=(
                            f"{entity} mapping records {local_code} appear "
                            f"{len(records)} times (source rows "
                            f"{[r.source_row for r in records]})"
                        ),
                        entity=entity,
                        source_file=records[0].source_file,
                        source_row=records[0].source_row,
                        account_code=local_code,
                        details={"source_rows": [r.source_row for r in records]},
                    )
                )

            # Conflicting mappings: the same local code resolves to more than
            # one distinct group code. Never auto-resolved.
            if len(group_codes) > 1:
                issues.append(
                    ValidationIssue(
                        code="conflicting_mapping",
                        severity=Severity.ERROR,
                        message=(
                            f"{entity} local account {local_code} maps to "
                            f"multiple group accounts {sorted(group_codes)}"
                        ),
                        entity=entity,
                        source_file=records[0].source_file,
                        source_row=records[0].source_row,
                        account_code=local_code,
                        details={"group_codes": sorted(group_codes)},
                    )
                )

            for record in records:
                if record.group_code not in self.group_coa:
                    issues.append(
                        ValidationIssue(
                            code="missing_group_account",
                            severity=Severity.ERROR,
                            message=(
                                f"{entity} local account {local_code} maps to "
                                f"group account {record.group_code!r}, which "
                                f"is not in the group chart of accounts"
                            ),
                            entity=entity,
                            source_file=record.source_file,
                            source_row=record.source_row,
                            account_code=local_code,
                            details={"group_code": record.group_code},
                        )
                    )
                self._check_name_mismatch(entity, record, issues)

        return issues

    def _check_name_mismatch(
        self, entity: str, record: MappingEntry, issues: list[ValidationIssue]
    ) -> None:
        """Flag a mapping whose group account contradicts an exact COA name.

        If a group account shares the mapping's ``local_name`` exactly, but the
        mapping points at a *different* group account, that is a probable
        mis-mapping worth a human review. Purely a name check against the COA —
        no account is hardcoded — so it fires only when the source data really
        contradicts itself (e.g. UK ``2310 Deferred income`` → 4100 while the
        COA keeps 'Deferred income' at 2300).
        """
        if not record.local_name:
            return
        wanted = record.local_name.strip().casefold()
        same_name = sorted(
            code
            for code, account in self.group_coa.items()
            if account.account_name.strip().casefold() == wanted
        )
        if same_name and record.group_code not in same_name:
            mapped = self.group_coa.get(record.group_code)
            issues.append(
                ValidationIssue(
                    code="group_account_name_mismatch",
                    severity=Severity.WARNING,
                    message=(
                        f"{entity} local account {record.local_code} "
                        f"({record.local_name!r}) maps to group account "
                        f"{record.group_code}"
                        f"{f' ({mapped.account_name!r})' if mapped else ''}, but "
                        f"the group chart has the same name at "
                        f"{same_name}"
                    ),
                    entity=entity,
                    source_file=record.source_file,
                    source_row=record.source_row,
                    account_code=record.local_code,
                    details={
                        "local_name": record.local_name,
                        "group_code": record.group_code,
                        "name_matched_group_accounts": same_name,
                    },
                )
            )

    # -- applying ----------------------------------------------------------

    def map_trial_balance(self, tb: TrialBalance) -> MappedTrialBalance:
        """Resolve every row of ``tb`` to a group account.

        US uses the identity mapping (local code == group code). UK/IN use
        their explicit mapping file. Rows whose local account has no mapping
        are still returned (never dropped) with ``mapped=False`` and an
        ``unmapped_account`` issue.
        """
        entity = tb.entity.strip().upper()

        if entity in _MAPPED_ENTITIES:
            entries, mapping_source_file = self.load_mapping(entity)
            lookup: dict[str, str] = {}
            # Only unambiguous records build the lookup; conflicting local
            # codes are left unresolved (reported, not guessed).
            by_local: dict[str, set[str]] = defaultdict(set)
            for entry in entries:
                if entry.local_code and entry.group_code:
                    by_local[entry.local_code].add(entry.group_code)
            for local_code, group_codes in by_local.items():
                if len(group_codes) == 1:
                    lookup[local_code] = next(iter(group_codes))

            issues = self.validate_mapping(entity, entries)
        else:
            # Identity mapping (US): the local code is already a group code.
            lookup = {}
            mapping_source_file = None
            issues = []

        rows: list[MappedRow] = []
        for row in tb.rows:
            group_code = self._resolve(entity, row.local_account_code, lookup)
            mapped = group_code is not None and group_code in self.group_coa
            group_account = self.group_coa.get(group_code) if group_code else None

            if entity not in _MAPPED_ENTITIES and group_code is not None and not mapped:
                issues.append(
                    ValidationIssue(
                        code="missing_group_account",
                        severity=Severity.ERROR,
                        message=(
                            f"{entity} account {row.local_account_code} is not "
                            f"in the group chart of accounts"
                        ),
                        entity=entity,
                        source_file=row.source_file,
                        source_row=row.source_row,
                        account_code=row.local_account_code,
                        details={"group_code": group_code},
                    )
                )
            if entity in _MAPPED_ENTITIES and group_code is None:
                issues.append(
                    ValidationIssue(
                        code="unmapped_account",
                        severity=Severity.WARNING,
                        message=(
                            f"{entity} local account {row.local_account_code} "
                            f"({row.account_name!r}) has no mapping to a group "
                            f"account"
                        ),
                        entity=entity,
                        source_file=row.source_file,
                        source_row=row.source_row,
                        account_code=row.local_account_code,
                    )
                )

            rows.append(
                MappedRow(
                    entity=entity,
                    source_file=row.source_file,
                    source_row=row.source_row,
                    local_account_code=row.local_account_code,
                    group_account_code=group_code if mapped else None,
                    group_account_name=group_account.account_name if group_account else None,
                    currency=row.currency,
                    debit=row.debit,
                    credit=row.credit,
                    cost_centre=row.cost_centre,
                    mapped=mapped,
                    raw=row.raw,
                )
            )

        return MappedTrialBalance(
            entity=entity,
            functional_currency=tb.functional_currency,
            source_file=tb.source_file,
            mapping_source_file=mapping_source_file,
            rows=rows,
            issues=issues,
        )

    @staticmethod
    def _resolve(
        entity: str, local_code: str, lookup: dict[str, str]
    ) -> str | None:
        if entity in _MAPPED_ENTITIES:
            return lookup.get(local_code)
        # US: identity, but only if the code is a real group account.
        return local_code or None
