# claude-rozi-sessions

Claude Code can run many background sessions from one client, each often in its own Git worktree.
rozi sees that client as a single pane. This extension lists every background session as its own
row in that pane, so rozi's Activity sidebar and Agents view show each session's state under the
repository and branch it works in.

```text
Activity
 rozi                                  master
 ⠋  Claude #1   worktree unlock
 rozi                     worktree-fix-login
 !  Claude #2   fix login redirect
 tui-lipan                              main
 ✓  Claude #3   focused_node_id docs
```

## Requirements

- rozi 0.0.28 or newer
- Claude Code with `claude agents --json`
- Python 3 available as `python`

## Install

From rozi's **Extensions…** palette entry, open the **Discover** tab and install
`claude-rozi-sessions`. Or use the CLI:

```bash
rozi extensions install https://github.com/tui-lipan/claude-rozi-sessions.git
rozi run-action reload-extensions
```

Later releases can be applied with `rozi extensions update claude-rozi-sessions`.

Start `claude` in a rozi pane and dispatch background sessions from it.

## How it works

The supervised `claude-rozi-sessions.watch` service polls two public commands:

- `claude agents --json`, for each session's state, name, and current directory;
- `rozi list-panes --format json`, for panes running Claude Code.

A pane running Claude Code whose foreground process is not itself one of the listed sessions is
showing Claude's session list. The service opens one `rozi publish` stream for that pane and
publishes one row per background session. Each row carries the session's directory, so rozi groups
it by that directory's project and branch. A linked worktree is labelled with its repository's
name.

Interactive sessions are not listed: each runs in a terminal of its own, which rozi already shows.
The extension does not need rozi's Claude Code plugin, and the plugin does not need it.

| Claude Code state | Row status |
| --- | --- |
| `working` | `working` |
| `needs_input`, `blocked`, and similar | `blocked` |
| `done` | `done` |
| `idle` | `idle` |
| `stopped` | `idle`, reason "Stopped" |
| `failed` | `idle`, reason "Session failed" |
| Anything else | `working` while Claude reports it busy, otherwise `idle` |

Selecting a row focuses the pane. Claude Code offers no way for another program to choose which
session its client shows, so switch sessions in Claude itself.

## Settings

Override the defaults in rozi's `config.toml`:

```toml
[extensions.claude-rozi-sessions]
claude = "claude"   # the Claude Code executable
poll_seconds = 2    # 0.5 to 60
scope = "all"       # or "cwd"
```

With `scope = "all"`, the pane lists every background session. With `scope = "cwd"`, it lists only
sessions started in or below the pane's directory. Use `cwd` when you run Claude's session list in
several panes for different projects; with `all`, each of those panes lists every session.

## Limits

- Only local panes are covered. A pane in a remote session has no process ID on this machine.
- The service runs only while a rozi client with the extension is attached.
- Rows have no `active` marker, because Claude Code does not report which session is on screen.

## Development

```bash
python -m unittest discover tests -p 'test_*.py'
rozi extensions check .
rozi extensions install --link .
```

## License

MPL-2.0. Contributions require a DCO sign-off.
