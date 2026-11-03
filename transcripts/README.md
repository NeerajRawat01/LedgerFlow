# AI tool transcripts

Full records of the AI coding sessions that produced LedgerFlow, exported from
the OpenCode session store (`~/.local/share/opencode/opencode.db`) on
2026-11-03. They are raw and unedited apart from secret redaction: dead ends,
wrong turns and the AI's own mistakes are left in, because those are the useful
part.

## What is here

- [`00-index.md`](00-index.md) — index of every session with message counts.
- `01-…` … `10-…` — one Markdown file per contributing session, in
  chronological order. Each file contains the full ordered turn list: user
  prompts, assistant reasoning, every tool call (with its input and output)
  and every patch.

These ten sessions (≈1,900 messages) are the complete set of AI-assisted
sessions for this project. Nine are on the LedgerFlow project
(`~/Desktop/LedgerFlow`); session 01 covers Phase 1 setup.

## Redaction

Only secrets are removed: Google `AIza…` keys, Groq `gsk_…` keys, OpenAI
`sk-…` keys, and the value of any `API_KEY` / `SECRET` / `TOKEN` /
`PASSWORD` / `AUTHORIZATION` assignment. Placeholders read `<REDACTED>`. The
redaction pass was applied to every file; no live credential remains in this
folder. Nothing else was changed — no tidying, no summarising.

## What is **not** available (documented gaps)

1. **No client-side export.** These are the stored session records, not a
   `/export` file. The store contains the same content (`session_message`
   rows), so the transcripts are genuine, but they were reconstructed from the
   database rather than exported by the tool.
2. **Deleted / non-OpenCode sessions.** Three Codex rollout files exist under
   `~/.codex/sessions/` but none relate to LedgerFlow; no Cursor chat export
   referencing LedgerFlow was found in `~/.cursor/`. Any LedgerFlow work done
   in those tools is **not recovered** and is not represented here.
3. **Post-compaction detail.** Two long sessions
   (`03-backend-implementation-audit`, `05-financial-consolidation-completion`)
   contain `compaction` records where the tool folded older turns into a
   summary. The summarised turns are preserved in the compaction record; the
   verbatim pre-compaction turns for those spans are no longer in the store.
4. **The submission author's own review notes** are not AI sessions and are
   not included.

## Regenerating

The export script reads the OpenCode SQLite store read-only. It is not needed
to read the transcripts; it is recorded only so the export is reproducible.

## Boundaries

Files here are working artifacts, not application data. Runtime state lives in
`storage/`, generated reports in `output/`, and the immutable assignment inputs
in `data/`.
