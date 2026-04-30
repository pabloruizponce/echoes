# Researcher Profile

Use this section when the user wants researcher-aware reranking or when `PROFILE.md` is missing, thin, or explicitly requested for update.

## Core Rules

- Treat `ECHOES_PROFILE` as the active profile path when set; otherwise use `<repo>/.echoes/PROFILE.md`.
- Treat the repo-root [PROFILE.md](../PROFILE.md) as a public template, not as the active filled profile.
- If the active profile already exists and contains real information, use it as-is unless the user explicitly asks for an update.
- Keep source evidence and active profiles in private runtime, not the repo root.
- Scripts collect evidence only. Codex synthesizes interests, exclusions, priorities, and ranking preferences.

## Guided Creation Workflow

1. Ask the user for researcher webpages or self-descriptions.
2. Ask for seed papers related to topics they want to learn about. Direct paper PDF URLs can be downloaded; paper/topic descriptions require Codex search and user confirmation before download.
3. Run `uv run python scripts/researcher_profile.py collect-evidence ...` for confirmed sources.
4. Read the evidence bundle and extracted paper text files, then write a short source brief for yourself.
5. Ask 3-6 clarifying questions with easy options and room for a free-form answer. Focus on topic priority, exclusions, broader vs narrower scope, methods/benchmarks, evidence style, and current learning goals.
6. Write the active profile as a flexible synthesis, not a rigid filled form.

## Evidence Collection

Use webpages as evidence, not ground truth. Capture stable facts from:

- personal, lab, project, or publication pages
- self-descriptions supplied by the researcher
- confirmed seed-paper PDFs and extracted text
- paper descriptions that still need search/confirmation

Do not infer deterministic keyword labels in the evidence bundle. Codex should read the sources and decide what matters.

## Clarifying Questions

Ask follow-up questions before writing the final profile when:

- sources point to multiple research directions
- academic interests and product goals conflict
- the evidence does not reveal what counts as a must-read paper
- sources are old and may not reflect the current agenda
- paper examples are adjacent but not clearly representative

Keep questions lightweight for the user: provide options and explicitly allow a free answer.

## File Shape

Keep `PROFILE.md` concise and human-readable. Use this general structure, adapting headings when the evidence calls for it:

- Identity And Role
- Current Research Direction
- Learning Priorities And Active Questions
- Useful Paper Signals
- Methods, Data, Benchmarks, And Evidence Preferences
- Usually Out Of Scope
- Source Evidence Reviewed
- Open Uncertainties Or Assumptions
- Last Updated

## Script Support

- Use `scripts/researcher_profile.py import-markdown --source /path/to/profile.md --overwrite` when the user already has a filled markdown profile and wants to make it the active private profile without rewriting it.
- Use `scripts/researcher_profile.py collect-evidence` to gather private source evidence.
- Use `scripts/researcher_profile.py init` only for a starter template or when deterministic formatting helps create a first draft.
- Do not overwrite an existing filled profile unless the user explicitly asked to update it.
- If public link inspection fails because of local TLS certificate issues, retry with `--insecure` only as a local fallback, or read the source with other available browsing tools.
- If the user only provides paper descriptions, Codex searches for likely papers and confirms candidates before passing confirmed PDF URLs to `collect-evidence`.
