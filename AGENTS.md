# claude-rozi-sessions agent guide

## Mission

Show every Claude Code background session as its own rozi Activity row, grouped by the worktree it
works in. The repository is a rozi extension: a manifest and one supervised Python service.

## Commands

- Unit tests: `python -m unittest discover tests -p 'test_*.py'`
- Manifest validation: `rozi extensions check .`
- Whitespace check: `git diff --check`

## Workflow rules

- Use only public interfaces: `claude agents --json`, `claude stop`, `claude plugin`,
  `git rev-parse`, Claude's `/resume` command, and rozi's documented CLI (`list-panes`,
  `capture-pane`, `send-text`, `send-keys`, `notify`, `publish`) and extension environment. Never
  read Claude's or rozi's private state files.
- Never type into Claude's prompt unless the screen shows an empty prompt, no dialog, no session
  list, and no streaming turn. Read the typed command back before pressing Enter, and do anything
  irreversible (`claude stop`) only after that read-back succeeds.
- Keep the service standard-library Python 3 with no third-party dependencies.
- An unrecognized Claude state must map to `working`, `idle`, `blocked`, or `done`; rozi reads any
  other status word as a live run.
- Keep `README.md` in sync with behavior, settings, and `min_rozi`.
- Bump `version` in `extension.toml` with every change users install: patch for a fix, minor for a
  feature or setting. rozi offers an update whenever the remote moves, and labels one without a new
  version only by its commit hash.
- Update this guide when durable repository conventions change.

## Commits

- Use Conventional Commits and include a DCO `Signed-off-by` trailer.
- Do not commit local configuration, credentials, runtime data, or `__pycache__`.
