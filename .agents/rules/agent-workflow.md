## Agent workflow: bugs, enhancements, and collaborating with the user

These rules apply to every agent and IDE (Claude Code, Cursor, Antigravity, others). They complement `AGENTS.md`; if they conflict, `AGENTS.md` wins inside a repo.

### Bugs and issues: find, validate, then fix

1. Locate the relevant code and form a hypothesis from the source before running anything.
2. Validate with the smallest targeted check (one test file/name, one spec, or direct inspection). If the issue can't be reproduced, say so instead of guessing or running broader suites.
3. Only then fix, and re-run that same targeted check to prove the fix.
4. Never open with a full test suite or full build. Widen only to the blast radius of the change. Run full suites only for shared/core code or when the user asks, and don't re-run a suite that already passed unless code changed.

### Enhancements: understand, ask, then build

1. Understand the relevant part of the application first (code, docs, current behavior).
2. Ask which area/feature should be enhanced and what specifically should change, unless the request already states both. Offer concrete options from what you found.
3. Do not implement until the user confirms the target and the desired change.

### Asking questions, options, and follow-ups

Plan together before building. Use the IDE's interactive question tool (e.g. `AskUserQuestion`, Cursor's ask-question tool) when available; otherwise present short numbered options in chat.

**When to ask:** the request is ambiguous or has several reasonable readings; an enhancement doesn't name both area and change; a bug's expected behavior is unclear; the change is hard to reverse or outward-facing (migrations, deletes, auth/permission or API contract changes, commits, pushes, deploys).
**When NOT to ask:** the answer is in the code, docs, or tests; there is a conventional default (pick it, state it in one line); the task is a simple, clearly-scoped fix. Don't interrogate the user on small tasks.

**How to ask well**
1. Do the homework first so options come from the real application, not generic ideas.
2. 1-4 focused questions per round, each with 2-4 concrete, mutually exclusive options (short label + meaning + trade-off). Recommended option first, marked "(Recommended)". Multi-select only when choices aren't exclusive.
3. Use previews (mockups, snippets, layout sketches) when options must be compared visually.
4. Make questions answerable in a click. Avoid "what do you want?" and "should I proceed?".
5. Never re-ask what was already stated.

**Follow-ups:** after each answer, check whether it opens a new decision (edge cases, roles/permissions, mobile vs desktop, error/empty states, existing data). If yes, ask a short follow-up round; if no, stop. Summarize the agreed plan (what, where, how, out of scope) and get a go-ahead before non-trivial work. If scope grows mid-task, pause and re-confirm.

**Suggest skills and help:** offer the fitting skill/workflow in one line and let the user choose; don't launch heavy workflows unasked. Use whatever equivalents the IDE provides:
- Bug/error: debug skill
- Unfamiliar area: explore-codebase
- UI/UX: ui-ux / frontend-design skills
- Architecture/design: architecture, system-design
- Test planning: testing-strategy
- Restructuring: refactor-safely, simplify
- Review before merge: code-review, security-review
- Release readiness: deploy-checklist
Also offer a quick plan, an options comparison, or a risk/impact summary.

**Tone:** concise and collaborative; a recommendation with each question, never an exhaustive survey; once the user decides, act without re-litigating.
