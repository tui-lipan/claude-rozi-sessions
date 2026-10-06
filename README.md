# claude-rozi-sessions

Claude Code can run many conversations from one client: its own, plus background sessions that
each often work in their own Git worktree. rozi sees that client as a single pane. This extension
lists every conversation as its own row in that pane, so rozi's Activity sidebar and Agents view
show each one's state under the repository and branch it works in. Selecting a row switches the
client to that conversation.

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
- Claude Code 2.1.285 or newer, for `claude agents --json` and `/resume` of a background session
- Python 3 available as `python`
- Linux or macOS. See [Limits](#limits).

## Install

From rozi's **Extensions…** palette entry, open the **Discover** tab and install
`claude-rozi-sessions`. Or use the CLI:

```bash
rozi extensions install https://github.com/tui-lipan/claude-rozi-sessions.git
rozi run-action reload-extensions
```

Later releases can be applied with `rozi extensions update claude-rozi-sessions`.

Start `claude` in a rozi pane and send work to the background from it.

For live status of the conversation on screen, also install
[rozi's Claude Code plugin](#with-rozis-claude-code-plugin): run **Install Claude Code hooks** from
the command palette.

## What is listed

The supervised `claude-rozi-sessions.watch` service polls `claude agents --json` and
`rozi list-panes`. A pane is a Claude client when Claude Code runs in it and rozi can read its
foreground process group. The client is in one of two modes:

- **Running its own conversation.** Claude lists that conversation as an interactive session whose
  process is in the pane's foreground process group. It is the active row. The pane publishes rows only when there is at least one background session besides it; a client alone
  with its own conversation is left to rozi's ordinary agent detection.
- **Attached.** After the client switches to a background session, or opens Claude's session list,
  it runs no conversation of its own. The active row is the session whose name Claude shows above
  its prompt, or the last one this extension switched to.

Every background session is listed too, each with the directory it works in, so rozi groups it by
that directory's project and branch. Interactive sessions in other terminals are listed where they
run, not here.

Rows are ordered by when each conversation started, the client's own included, and a row keeps the
place it was first published in. rozi names a pane's rows by position (`Claude Code #2`), so
switching never renumbers them. A conversation that moves, such as the client's own one going to
the background, keeps its row: its ID, its place, and the run history rozi keeps for it.

| Claude Code reports | Row status |
| --- | --- |
| `status` `waiting`: a permission prompt, question, or other dialog is open | `blocked`, with Claude's reason, such as "Permission prompt" |
| `working` | `working` |
| `blocked` with no dialog open | `done`, reason "Awaiting your reply" |
| `done` | `done` |
| `idle` | `idle` |
| `stopped` | `idle`, reason "Stopped" |
| Stopped by a switch, see [Switch sessions](#switch-sessions) | `done` after a finished turn, otherwise `idle`; reason "Stopped" |
| `failed` | `idle`, reason "Session failed" |
| Anything else | `working` while Claude reports it busy, otherwise `idle` |

Claude also reports `blocked` once a turn has finished, when its reply ends with a question or
asks you to choose, and for a session waiting for its first prompt. No dialog is open then, so the
row shows the turn as `done` and keeps `blocked` for a session that cannot continue until you
answer it.

### With rozi's Claude Code plugin

rozi's Claude Code hook plugin reports the state of the conversation the client runs itself. Each
row carries its conversation ID as `native_session`, and rozi lets the hook report drive the row
with the same ID, so that row shows the hooks' live state while every other row stays listed.

Claude runs no hooks in the pane when the client attaches to a background session, so the hooks'
last report goes stale. rozi keeps it out of the list once no row names its conversation, and
restores the active row's conversation when the session is resurrected.

The plugin is optional, and this extension never installs it on its own: it changes Claude Code's
setup for every session, inside rozi or not. When Claude is in use and the plugin is not installed,
the extension says so in one notification per run. To install it, run **Install Claude Code
hooks** from the command palette, or `rozi run-action claude-rozi-sessions.install-hooks`. That
runs Claude Code's own commands, the same as installing it by hand:

```bash
claude plugin marketplace add tui-lipan/rozi
claude plugin install rozi@rozi
```

Then restart Claude Code in its panes. A plugin named `rozi` from any marketplace counts as
installed, enabled or not, so a disabled one is not suggested again. Set `suggest_hooks = false`
to never mention it.

## Switch sessions

Selecting a row focuses the pane, and then this extension switches the client to that
conversation. It types `/resume <session-id>` into Claude's prompt, reads it back from the screen,
and only then presses Enter.

- **From the client's own conversation**, `/resume` moves that conversation to the background,
  where it keeps running, and attaches the selected one. Claude lists it there under a new session
  ID while keeping its transcript under the first, so the extension keeps its row under the first
  ID, and resumes it by that ID when it is selected again.
- **From an attached client**, Claude refuses to resume a session that is still running. The
  extension follows Claude's own advice and runs `claude stop <id>` on the selected session first,
  then resumes it in place. The conversation that was on screen stops, saved and resumable, and
  stays listed as a stopped row so it can be selected again.

The extension does not switch, and says why in a rozi notification, when:

- Claude's prompt holds unsent text. Claude's dim placeholder does not count.
- Claude is showing a question, an approval dialog, or any screen without its prompt.
- Claude is showing its own session list, whose prompt starts a new session.
- From an attached client, the selected session is still working. Stopping it would cut its turn
  short.
- The selected session works in another repository than the Claude client. Claude's `/resume`
  finds only the client's own repository's sessions, in its directory or another Git worktree of
  the same repository, so the extension never stops or types anything for it. Open it with ← in
  Claude, or with `claude attach <id>`.
- It cannot tell which session an attached client shows, because no name matches exactly one row.
- The selected session has ended.

While Claude is responding, the switch does not interrupt it. Claude queues the command and runs
it once the turn ends, and the extension says so in a notification and follows the queued switch
until it happens: the conversation on screen finishes its turn first. Selecting another row
meanwhile is refused, so switches do not pile up in Claude's queue. Clearing the queue in Claude
cancels the switch.

If the command does not read back exactly, for example because someone typed at the same moment,
it is left in the prompt unsent. When the selected session has to be stopped first, the prompt is
read again after stopping it, right before Enter; if it changed, nothing is sent and the stopped
session stays listed to be selected again.

A switch counts only once Claude names the selected session above its prompt: its name, or its
short ID when it has none. A label that names another session means the switch did not happen.
Claude draws no name right after a switch from the client's own conversation, so there the switch
counts once that conversation leaves the client and Claude has refused nothing.
When the name is shared, an attached client's switch is confirmed instead by the session now
running in the worker that showed the conversation it replaced. If Claude refuses, the notification
quotes Claude's answer. A conversation Claude cannot find, such as one that never got a first
prompt, is no longer listed. If there is no confirmation within ten seconds of sending the switch,
or of a queued switch's turn ending, or a queued switch waits more than 30 minutes, a notification
says so, and the active row keeps following what Claude itself shows.

To list sessions without ever typing into Claude, set `switching = false`.

## Settings

Override the defaults in rozi's `config.toml`:

```toml
[extensions.claude-rozi-sessions]
claude = "claude"   # the Claude Code executable
poll_seconds = 2    # 0.5 to 60
scope = "all"       # or "cwd"
switching = true    # false: never type into Claude
suggest_hooks = true  # false: never suggest rozi's Claude Code plugin
```

With `scope = "all"`, the pane lists every background session on the machine, like Claude's own
session list. With `scope = "cwd"`, it lists only sessions started in or below the pane's
directory. Use `cwd` when you run Claude clients in several panes for different projects; with
`all`, each of those panes lists every session.

## Limits

- **Windows.** rozi reports no foreground process group there, and Python cannot read one, so no
  pane is recognised as a Claude client. Rows are not published.
- **Remote panes.** rozi omits the foreground process group for a remote attachment, whose
  processes are on another machine, so those panes are left alone.
- **Sessions of other repositories** are listed with `scope = "all"` but cannot be switched to from
  this pane, because Claude resumes only its own repository's sessions. Use `scope = "cwd"` to
  list only the pane's own.
- **Switching between running sessions from an attached client** stops the selected session first.
  Claude offers no supported way to attach another running session from a client that is already
  attached, other than navigating its session list by hand with ←.
- **The active row of an attached client** comes from the session name Claude draws above its
  prompt. Two sessions with the same name make it ambiguous; the last switch made here is used
  instead.
- Switching reads Claude's screen. A future Claude release that redraws its prompt differently
  makes the extension refuse to switch rather than guess.
- The service runs only while a rozi client with the extension is attached. Its errors go to a
  stream rozi discards, so a missing `claude` shows up as no rows rather than as a message.

## Development

```bash
python -m unittest discover tests -p 'test_*.py'
rozi extensions check .
rozi extensions install --link .
```

## License

MPL-2.0. Contributions require a DCO sign-off.
