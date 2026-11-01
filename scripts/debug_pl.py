#!/usr/bin/env python3
"""Debug the engine's per-entity translated P&L to locate the 6500 gap."""
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.accounting.statements import StatementAssembler
from app.services.source_data import SourceDataService

svc = SourceDataService(str(ROOT / "data"))
asm = StatementAssembler(svc)

# Load entities and journals the way assembly does.
entity_data = asm._load_entities(["UK", "US", "IN"])
asm._entity_data = entity_data
decisions = asm.journals.validate()
from app.accounting.adjustments import JournalStatus
posted = {d.journal_id for d in decisions if d.status is JournalStatus.POSTED}
posted_journals = [j for j in asm.journals.load_journals() if j.journal_id in posted]
print("POSTED:", sorted(posted))
adjusted = asm._apply_journals(entity_data, posted_journals)
translated = asm._translate(entity_data, adjusted)

for ent in ["UK", "US", "IN"]:
    pl = translated[ent]["pl"]
    print(f"{ent} 6500 =", pl.get("6500"), " rate_used=", translated[ent]["income_rate"], "closing=", translated[ent]["closing_rate"])

# Which journals touch 6500 in each entity?
for j in posted_journals:
    for line in j.lines:
        # resolve mapping
        lookup = {}
        # build from mapped rows
        data = entity_data[j.entity]
        for row in data.mapped.rows:
            if row.mapped and row.group_account_code:
                lookup.setdefault(row.local_account_code, row.group_account_code)
        g = lookup.get(line.account)
        if g == "6500":
            print(f"  {j.journal_id} {j.entity} acct {line.account} -> {g} dr={line.debit} cr={line.credit}")
