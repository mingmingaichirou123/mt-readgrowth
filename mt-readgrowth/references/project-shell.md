# MT-readgrowth project shell contract

## Purpose

Bind one Agent project to the user-level installed `mt-readgrowth` Skill without placing capability code or user data in the same directory. The shell removes the need to name the Skill in every new conversation while preserving explicit user control over profile and belief writes.

## Fixed structure

```text
<project-root>/
├─ AGENTS.md
└─ reading-workspace/
   ├─ workspace.yaml
   └─ knowledge/user-profile/settings.json
```

Open `<project-root>` in the Agent. Do not open `reading-workspace/` as the project root. Keep the installed Skill in the host's user-level Skill directory and resolve runtime files from its own `SKILL.md`.

## Initialize

From the installed Skill directory, run:

```text
<python-3.10+> scripts/project_shell.py init --project-root <project-root>
```

The command:

- refuses a project path inside the installed Skill;
- refuses linked or escaping `AGENTS.md` and `reading-workspace` targets;
- stops before workspace initialization when an existing `AGENTS.md` has no exact managed binding;
- creates the fixed managed Agent guide only when it is absent;
- initializes `reading-workspace/` through the existing idempotent workspace initializer;
- preserves user-authored Agent instructions outside the exact managed block;
- creates no reading data, user information, beliefs, credentials, or Skill copy.

An existing managed block that differs from the installed template is a review gate, not permission to replace it. The user must reconcile the project guide explicitly.

## Audit each new task

Before the first workspace read or write in each new task, run:

```text
<python-3.10+> scripts/project_shell.py audit --project-root <project-root>
```

Require `result: valid`. The audit verifies:

- the exact managed binding still exists in `AGENTS.md`;
- `reading-workspace/` is the direct, non-linked child of the project root;
- `workspace.yaml` matches the fixed MT-readgrowth contract;
- every standard workspace directory exists and does not escape through a link.

The audit must not enumerate or read books, sessions, user-profile events, beliefs, weekly reports, credentials, or other personal content. A valid shell binds routing and location only; it does not authorize a reading operation or personal-data access.

## Memory boundary

Automatic Skill association is independent from memory authorization. Continue to enforce the workspace mode:

- `explicit-only`: write profile information only after an explicit current remember/update/forget request;
- `proactive-review`: materialize only reviewed session candidates and never confirm them silently;
- `off`: do not load or write profile state.

Long-term belief transitions continue to require their existing independent confirmations. Opening the project, accepting a workflow, or seeing `MT-readgrowth active` is not a profile or belief decision.

## Host boundary

The bundled shell currently targets Codex and other hosts that automatically load project-root `AGENTS.md`. A host that does not honor `AGENTS.md` still needs its own thin project-instruction bridge or one explicit Skill selection at the start of a conversation. Do not claim universal automatic binding from the standalone Skill alone.
