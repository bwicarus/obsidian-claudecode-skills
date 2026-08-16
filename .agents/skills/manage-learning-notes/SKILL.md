---
name: manage-learning-notes
description: Run, inspect, or troubleshoot this project's Obsidian learning-note pipeline, including new-note registration, PDF and image annotation, summary and index updates, related-note linking, Anki card creation or status refresh, and optional knowledge-graph refresh. Use when the user says 登记新笔记, /summarize, /connect, /pdf-mark, /img-mark, /anki, asks to process a learning note in C:\obsidian, or asks why one of those steps failed. Do not use for assistant-only project notes under AI助手专用; use obsidian-project-notes for those.
---

# Manage learning notes

Use the existing orchestrator and project rules. Do not reconstruct the old multi-step Claude workflow in the prompt.

## Route the request

1. Distinguish the main learning vault from `C:\obsidian\AI助手专用`.
   - For assistant project notes, stop and use `obsidian-project-notes`.
   - For learning notes, continue here.
2. Respect the requested scope.
   - An explicit note path means process only that note.
   - “登记新笔记” or a request to scan changed notes means use the batch orchestrator.
   - A request to explain or diagnose is read-only until the user asks to run or repair it.
3. Use the current Python interpreter required by this repository:
   `C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`.

## Preflight AI-backed stages

Before `img`, `summarize`, `connect`, `anki`, `all`, or a PDF extraction that may fall back to vision:

1. Inspect `scripts/config.py`, `scripts/ai_client.py`, and only the `backend`/`model` fields of the configured settings file. Do not print credentials or unrelated settings.
2. Resolve the selected CLI with `Get-Command claude` or `Get-Command codex` and verify the configured executable path exists.
3. On Windows, do not assume `auto-claude` or `auto-codex` survives a missing primary executable: the current resolver does not fall back to `PATH` there, and a launch error occurs before rate-limit fallback.
4. If the configured path is stale but the same selected CLI is available on `PATH`, set `APP_CLAUDE` or `APP_CODEX` only for the invoked process. Do not silently change the saved backend or rewrite user settings.
5. If the selected backend cannot be launched, stop before a note mutation and report the failed prerequisite.

## Run the supported entry points

Use `scripts/register_notes.py` as the source of truth for the integrated workflow. Check its current `--help` before assuming flags.

```powershell
& 'C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe' `
  'C:\claude\scripts\register_notes.py'

& 'C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe' `
  'C:\claude\scripts\register_notes.py' --note '<note-path>' --only <pdf|img|summarize|connect|anki|all>
```

The orchestrator currently may refresh the matching knowledge graph even after a single-stage run. For an explicit `/summarize`, `/connect`, `/img-mark`, `/pdf-mark`, or `/anki` request, add `--no-update-kg` unless the user also asked to refresh KG. Let the complete registration workflow keep its normal KG behavior.

Use the single-link extractor only for an Obsidian PDF-region link:

```powershell
& 'C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe' `
  'C:\claude\scripts\pdf_extract.py' --link '<obsidian-pdf-link>'
```

For batch PDF annotation, preview before a requested write:

```powershell
& 'C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe' `
  'C:\claude\scripts\annotate_note.py' --note '<note-path>' --dry-run
```

Then run the same command without `--dry-run` only when the requested operation includes modifying the note.

For an explicit Anki status refresh, use the current `scripts/anki_status.py --help`, then run only the requested note or explicitly requested batch. Status refresh can start Anki and writes frontmatter or records only when the corresponding flags are present.

## Load only the relevant rules

- Read `../../../references/pdf-annotation-format.md` for PDF or image annotation format.
- Read `../../../references/anki-selection-rules.md` and `../../../references/anki-card-format.md` for Anki generation or review.
- Inspect the active prompt under `../../../references/prompts/` only when diagnosing AI output or changing that stage.
- Treat `scripts/register_notes.py` and the scripts it imports as behavior authority when an old `.claude/skills/*.md` description disagrees.
- For image annotation, inspect both the active prompt and the actual attachment path. The current prompt's mention of a model-side image reader is not proof that `register_notes.py` attached the image to the selected backend; verify one representative image before claiming this stage works.

## Preserve project semantics

- Do not send `anki/records/` or `state/note-states.json` to an AI model; they are script-owned state.
- Preserve section-level hashes and recorded skip decisions so unchanged sections do not create duplicate cards.
- Preserve existing related-note links when the script uses merge semantics; do not hand-edit indexes as a shortcut.
- Report which note or scan scope ran, which stages changed data, which stages skipped, and any failure without claiming later stages completed.

## Verify

- Require a zero exit code from the invoked script.
- For a write, inspect only the explicitly affected note, index, or record and confirm the expected block or metadata appeared once.
- Do not start Anki, Obsidian, a batch scan, or an external sync merely to answer a read-only question.
