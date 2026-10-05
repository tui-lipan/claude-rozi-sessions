#!/usr/bin/env python3
"""Publish every Claude Code conversation behind one client as a rozi Activity row, and switch the
client to a row's conversation when the row is selected.

Claude Code runs background sessions under its own daemon, each often in its own Git worktree,
and one client shows them. rozi sees that client as one pane. This service asks Claude for the
sessions it runs, publishes one row per session into the pane with the directory it works in, and
answers a row activation by switching the client to that conversation through Claude's own
`/resume` command.

Everything goes through public interfaces: `claude agents --json`, `claude stop`,
`claude plugin list --json`, and rozi's `list-panes`, `capture-pane`, `send-text`, `send-keys`,
`notify`, and `publish`.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable


ROZI = os.environ.get("ROZI_BIN", "rozi")
EXTENSION_ID = "claude-rozi-sessions"
COMMAND_TIMEOUT = 5.0
# rozi's Claude Code plugin: its hooks report the live state of the conversation a client shows.
HOOKS_PLUGIN = "rozi"
HOOKS_MARKETPLACE = "tui-lipan/rozi"
HOOKS_NOTICE = (
    "For live status of the conversation on screen, install rozi's Claude Code hooks: run "
    "“Install Claude Code hooks” from the command palette. suggest_hooks = false hides this."
)
# How long to wait for Claude to show the selected session after `/resume`: Claude resumes a
# conversation in a second or two, and a stopped one can take longer to load.
CONFIRM_ATTEMPTS = 20
CONFIRM_INTERVAL = 0.5
# How long to wait for the typed command to read back from Claude's prompt before giving up.
VERIFY_ATTEMPTS = 10
VERIFY_INTERVAL = 0.2
# How long after a client's own conversation leaves it to wait for Claude to list that
# conversation again as a background session, under its new id.
MOVE_WINDOW_MS = 30_000
# Anything unrecognized falls back on its coarse busy/idle `status`, never on a custom word, which
# rozi would read as a live run.
FAILED_STATES = {"failed", "error", "errored", "crashed"}
# Shown by Claude in place of a conversation's prompt: its list of every session, whose prompt
# starts a *new* session. Typing a command there would create one.
AGENT_VIEW_MARKERS = (
    "describe a task for a new session",
    "Your conversation moved to the background",
    "enter to return",
)
# A turn still streaming. The footer hint is shown while the prompt is empty, which is the only
# state a switch proceeds from; the activity line covers a turn whose hint has scrolled away.
WORKING_FOOTER = "esc to interrupt"
WORKING_LINE = re.compile(r"^\s*[*·✢✶✻✽]\s+\S.*…(?:\s+\(\d+[smh](?:\s|·)|\s*$)")
# Claude's answers to a `/resume` it did not carry out.
SWITCH_FAILURES = (
    "was not found",
    "is running in the background",
    "No conversation found",
)
# The refusals that mean the conversation cannot be resumed at all.
GONE_FAILURES = ("was not found", "No conversation found")
PROMPT = "❯"
RULE = "─"


class SessionsError(RuntimeError):
    """A public CLI call failed or answered something unreadable."""


@dataclass(frozen=True)
class Settings:
    claude: str = "claude"
    poll_seconds: float = 2.0
    scope: str = "all"
    switching: bool = True
    suggest_hooks: bool = True

    @classmethod
    def from_environment(cls, environ: dict[str, str]) -> Settings:
        try:
            values = json.loads(environ.get("ROZI_EXTENSION_CONFIG") or "{}")
        except json.JSONDecodeError:
            values = {}
        if not isinstance(values, dict):
            values = {}
        claude = values.get("claude")
        scope = values.get("scope")
        switching = values.get("switching")
        suggest_hooks = values.get("suggest_hooks")
        try:
            poll = float(values.get("poll_seconds", cls.poll_seconds))
        except (TypeError, ValueError):
            poll = cls.poll_seconds
        return cls(
            claude=claude if isinstance(claude, str) and claude.strip() else cls.claude,
            poll_seconds=min(max(poll, 0.5), 60.0),
            scope=scope if scope in {"all", "cwd"} else cls.scope,
            switching=switching if isinstance(switching, bool) else cls.switching,
            suggest_hooks=(
                suggest_hooks if isinstance(suggest_hooks, bool) else cls.suggest_hooks
            ),
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    short_id: str
    pid: int | None
    kind: str
    cwd: str | None
    name: str | None
    state: str
    status: str
    waiting_for: str | None
    started_at: int
    # Stopped by this service to make way for another conversation, so Claude no longer lists it.
    stopped: bool = False

    @classmethod
    def from_wire(cls, value: object) -> Session | None:
        if not isinstance(value, dict):
            return None
        session_id = text(value.get("sessionId")) or text(value.get("id"))
        if session_id is None:
            return None
        return cls(
            session_id=session_id,
            short_id=text(value.get("id")) or session_id[:8],
            pid=integer(value.get("pid")),
            kind=(text(value.get("kind")) or "").casefold(),
            cwd=text(value.get("cwd")),
            name=text(value.get("name")),
            state=(text(value.get("state")) or "").casefold(),
            status=(text(value.get("status")) or "").casefold(),
            waiting_for=text(value.get("waitingFor")),
            started_at=integer(value.get("startedAt")) or 0,
        )

    def row_state(self) -> tuple[str, str | None]:
        """The rozi status this session shows, and why when the word alone does not say."""
        if self.stopped:
            # A finished run stays finished, so the row keeps its result and run time.
            status, _ = replace(self, stopped=False).row_state()
            return ("done" if status == "done" else "idle"), "Stopped"
        # Only a live process showing a dialog reports `waiting`. Claude also says `blocked` when a
        # finished turn's reply reads as a question; that turn is over, and nothing is stuck.
        if self.status == "waiting":
            return "blocked", (self.waiting_for or "").capitalize() or None
        if self.state == "blocked":
            return "done", "Awaiting your reply"
        if self.state in FAILED_STATES:
            return "idle", "Session failed"
        if self.state == "stopped":
            return "idle", "Stopped"
        if self.state in {"working", "done", "idle"}:
            return self.state, None
        return ("working" if self.status == "busy" else "idle"), None

    def is_working(self) -> bool:
        return self.row_state()[0] == "working"

    def row(self, active: bool, conversation: str | None = None) -> dict[str, object]:
        """This session's row. A background session continuing a conversation started elsewhere
        is published under that `conversation`, the id Claude resumes it by."""
        status, reason = self.row_state()
        conversation = conversation or self.session_id
        row: dict[str, object] = {
            "id": conversation,
            "title": self.name or "",
            "status": status,
            "active": active,
            # Ties the row to the conversation Claude's own hooks report on, if they run, and is
            # what rozi resumes the pane with.
            "native_session": conversation,
        }
        if reason:
            row["reason"] = reason
        if self.cwd:
            row["cwd"] = self.cwd
        return row


@dataclass(frozen=True)
class Pane:
    pane_id: int
    cwd: str | None
    program: str | None
    agent: str | None
    foreground_pid: int | None
    session: str | None = None

    @classmethod
    def from_wire(cls, value: object) -> Pane | None:
        if not isinstance(value, dict):
            return None
        pane_id = integer(value.get("id"))
        if pane_id is None:
            return None
        return cls(
            pane_id=pane_id,
            cwd=text(value.get("cwd")),
            program=text(value.get("foreground_program")),
            agent=text(value.get("agent")),
            foreground_pid=integer(value.get("foreground_pid")),
            session=text(value.get("session")),
        )

    def runs_claude(self) -> bool:
        return self.agent == "claude" or (self.program or "").casefold() in {
            "claude",
            "claude-code",
        }


@dataclass(frozen=True)
class Host:
    """A pane running a Claude client, and what that client is.

    `interactive` is the conversation the client runs itself, when it runs one: `/resume` from
    there moves it to the background and attaches another. Without one the client is attached to
    a background session (or showing Claude's session list), and Claude refuses to `/resume` a
    running session from there.
    """

    pane: Pane
    interactive: Session | None


@dataclass
class HostMemory:
    """What the service remembers about one host pane between polls."""

    # The session this service last switched the client to, for when the screen does not say.
    last_target: str | None = None
    # Conversations a switch stopped to take their place, kept listed so they can be switched back
    # to. Claude no longer lists a stopped session as running.
    displaced: dict[str, Session] = field(default_factory=dict)
    published: list[dict[str, object]] | None = None
    # The client's own conversation as last seen, kept after it leaves the client until Claude
    # lists it again in the background, and when it left.
    own: Session | None = None
    own_left_at: int | None = None
    # Every session id the previous poll listed.
    seen: set[str] = field(default_factory=set)
    # Background sessions that continue a conversation this pane listed under another id, mapped to
    # that conversation. Claude keeps the transcript under the first id and resumes it only by that
    # id, and the row keeps it too, with its place, so rozi keeps the row's history.
    moved: dict[str, Session] = field(default_factory=dict)
    # Where each published row sorts: when its conversation started, as first seen. A
    # conversation resumed in another worker reports that worker's start, which must not move it.
    places: dict[str, int] = field(default_factory=dict)

    def origin(self, session: Session) -> Session:
        """The conversation `session`'s row was first published for."""
        return self.moved.get(session.session_id, session)

    def session_for_row(self, row_id: str) -> str:
        """The session a published row stands for now."""
        for session_id, origin in self.moved.items():
            if origin.session_id == row_id:
                return session_id
        return row_id


@dataclass(frozen=True)
class Composer:
    text: str
    """What the user has typed, with Claude's dim placeholder left out."""


@dataclass(frozen=True)
class Screen:
    agent_view: bool
    composer: Composer | None
    working: bool
    label: str | None

    @property
    def draft(self) -> bool:
        return self.composer is not None and bool(self.composer.text.strip())


def text(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def process_group(pid: int | None) -> int | None:
    """The process group `pid` runs in, which is what rozi reports as a pane's `foreground_pid`.

    A launcher such as a version-manager shim or `npx` leads the group while Claude runs inside
    it, so comparing raw process IDs would miss it. Unavailable on Windows.
    """
    if pid is None or not hasattr(os, "getpgid"):
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def parse_sessions(output: str) -> list[Session]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise SessionsError("claude agents --json returned invalid JSON") from error
    if not isinstance(data, list):
        raise SessionsError("claude agents --json returned a non-list payload")
    return [session for item in data if (session := Session.from_wire(item)) is not None]


def has_hooks_plugin(output: str) -> bool:
    """Whether `claude plugin list --json` lists rozi's plugin, from any marketplace, enabled or
    not: a disabled one was turned off on purpose."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise SessionsError("claude plugin list --json returned invalid JSON") from error
    if not isinstance(data, list):
        raise SessionsError("claude plugin list --json returned a non-list payload")
    return any(
        isinstance(item, dict) and (text(item.get("id")) or "").split("@")[0] == HOOKS_PLUGIN
        for item in data
    )


def response_data(output: str, command: str) -> Any:
    try:
        response = json.loads(output)
    except json.JSONDecodeError as error:
        raise SessionsError(f"rozi {command} returned invalid JSON") from error
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise SessionsError(str(detail or f"rozi {command} failed"))
    return response.get("data")


def parse_panes(output: str) -> list[Pane]:
    data = response_data(output, "list-panes")
    if not isinstance(data, list):
        raise SessionsError("rozi list-panes returned a non-list payload")
    return [pane for item in data if (pane := Pane.from_wire(item)) is not None]


def classify(
    panes: list[Pane],
    sessions: list[Session],
    group_of: Callable[[int | None], int | None] = process_group,
) -> list[Host]:
    """Panes running a Claude client, each with the conversation it runs itself, if any.

    A pane without a readable foreground process group cannot be matched against Claude's
    sessions and is left to rozi's own detection: every Windows pane, and every remote one.
    """
    hosts = []
    for pane in panes:
        if not pane.runs_claude() or pane.foreground_pid is None:
            continue
        own = next(
            (
                session
                for session in sessions
                if session.kind == "interactive"
                and session.pid is not None
                and pane.foreground_pid in {session.pid, group_of(session.pid)}
            ),
            None,
        )
        hosts.append(Host(pane=pane, interactive=own))
    return hosts


def is_under(path: str | None, root: str | None) -> bool:
    if not path or not root:
        return False
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def listed_sessions(host: Host, sessions: list[Session], scope: str) -> list[Session]:
    """The conversations one host pane lists: its own and the background ones.

    Interactive sessions in other terminals are left out; each is listed where it runs.
    """
    background = [
        session
        for session in sessions
        if session.kind == "background"
        and (scope == "all" or is_under(session.cwd, host.pane.cwd))
    ]
    own = [host.interactive] if host.interactive is not None else []
    return own + background


def active_session(
    host: Host, listed: list[Session], label: str | None, last_target: str | None
) -> str | None:
    """The conversation the client has on screen.

    A client running its own conversation shows it. An attached client names the session it
    shows above its prompt; that name must match exactly one listed session. Failing both, the
    last switch this service made stands, while that session is still listed.
    """
    if host.interactive is not None:
        return host.interactive.session_id
    if label:
        matches = [
            session
            for session in listed
            if session.name == label or session.short_id == label or session.session_id == label
        ]
        if len(matches) == 1:
            return matches[0].session_id
    if last_target and any(session.session_id == last_target for session in listed):
        return last_target
    return None


def follow_moved(memory: HostMemory, host: Host, sessions: list[Session], now_ms: int) -> None:
    """Notice the client's own conversation reappearing in the background under a new id.

    `/resume` from a client's own conversation moves it to the background, where Claude lists it
    as a new session. The one new background session in the same directory, appearing soon after,
    is that conversation, and keeps its row.
    """
    if host.interactive is not None:
        memory.own = host.interactive
        memory.own_left_at = None
    elif memory.own is not None:
        if memory.own_left_at is None:
            memory.own_left_at = now_ms
        fresh = [
            session
            for session in sessions
            if session.kind == "background"
            and session.session_id not in memory.seen
            and session.cwd == memory.own.cwd
            and session.started_at >= memory.own.started_at
        ]
        if len(fresh) == 1:
            memory.moved[fresh[0].session_id] = memory.origin(memory.own)
            memory.own = None
        elif now_ms - memory.own_left_at > MOVE_WINDOW_MS:
            memory.own = None
    memory.seen = {session.session_id for session in sessions}
    for session_id in list(memory.moved):
        if session_id not in memory.seen and session_id not in memory.displaced:
            del memory.moved[session_id]


def host_rows(
    host: Host,
    sessions: list[Session],
    scope: str,
    memory: HostMemory,
    label: str | None,
) -> list[dict[str, object]]:
    """The rows a host pane publishes, or none when it has nothing a single row could not say.

    A client running only its own conversation is one agent, which rozi already shows; the hooks
    or screen detection speak for it better than a two-second poll.

    Rows are ordered by when each conversation started, wherever it runs now, so switching never
    renumbers them: rozi names a pane's rows by position.
    """
    listed = listed_sessions(host, sessions, scope)
    running = {session.session_id for session in listed}
    for session_id, session in list(memory.displaced.items()):
        # Running again, under its own id or, once resumed, under the conversation's first one.
        if session_id in running or memory.origin(session).session_id in running:
            del memory.displaced[session_id]
    displaced = [
        replace(session, stopped=True, kind="background") for session in memory.displaced.values()
    ]
    everything = listed + displaced
    if host.interactive is not None and len(everything) < 2:
        return []
    def place(session: Session) -> tuple[int, str]:
        origin = memory.origin(session)
        return memory.places.setdefault(origin.session_id, origin.started_at), origin.session_id

    everything.sort(key=place)
    memory.places = {
        row_id: started
        for row_id, started in memory.places.items()
        if any(memory.origin(session).session_id == row_id for session in everything)
    }
    active = active_session(host, everything, label, memory.last_target)
    return [
        session.row(session.session_id == active, memory.origin(session).session_id)
        for session in everything
    ]


def row_text(row: list[dict[str, Any]]) -> str:
    return "".join(span.get("text", "") for span in row)


def is_rule(line: str) -> bool:
    """A full-width horizontal rule, plain or carrying a session name (`──── name ─`)."""
    line = line.strip()
    return bool(line) and (set(line) == {RULE} or rule_label(line) is not None)


def composer_span(rows: list[list[dict[str, Any]]]) -> tuple[int, int] | None:
    """Where the prompt is: the rows between the last two rules whose first row starts with `❯`,
    as a `[start, end)` range. `None` when there is no prompt, as under a dialog."""
    texts = [row_text(row) for row in rows]
    rules = [index for index, line in enumerate(texts) if is_rule(line)]
    for upper, lower in zip(reversed(rules[:-1]), reversed(rules[1:])):
        if lower > upper + 1 and texts[upper + 1].lstrip().startswith(PROMPT):
            return upper + 1, lower
    return None


def read_composer(body: list[list[dict[str, Any]]]) -> Composer:
    """What the user has typed: every span that is not Claude's dim placeholder, minus the `❯`."""
    typed = []
    for index, row in enumerate(body):
        for span in row:
            content = span.get("text", "")
            if index == 0 and content.lstrip().startswith(PROMPT):
                content = content.lstrip()[len(PROMPT) :]
            if span.get("dim"):
                continue
            typed.append(content)
        typed.append("\n")
    return Composer(text="".join(typed).replace("\xa0", " ").strip())


def rule_label(line: str) -> str | None:
    match = re.match(rf"^{RULE}+ (.+?) {RULE}+$", line.strip())
    return match.group(1).strip() if match else None


def read_screen(frame_rows: list[list[dict[str, Any]]]) -> Screen:
    texts = [row_text(row) for row in frame_rows]
    joined = "\n".join(texts)
    span = composer_span(frame_rows)
    return Screen(
        agent_view=any(marker in joined for marker in AGENT_VIEW_MARKERS),
        composer=read_composer(frame_rows[span[0] : span[1]]) if span else None,
        working=WORKING_FOOTER in joined or any(WORKING_LINE.match(line) for line in texts),
        label=rule_label(texts[span[0] - 1]) if span else None,
    )


def switch_failure(screen_text: str, command: str) -> str | None:
    """Claude's refusal of `command`, read only below the last place it echoed that command, so an
    older refusal still on screen is not mistaken for this one."""
    lines = screen_text.splitlines()
    echoed = [index for index, line in enumerate(lines) if command in line]
    if not echoed:
        return None
    for line in lines[echoed[-1] + 1 :]:
        if any(failure in line for failure in SWITCH_FAILURES):
            return line.strip(" ⎿\xa0")
    return None


def text_label(screen_text: str) -> str | None:
    """The session name an attached client shows above its prompt, from a plain-text capture."""
    rows = [[{"text": line}] for line in screen_text.splitlines()]
    span = composer_span(rows)
    return rule_label(screen_text.splitlines()[span[0] - 1]) if span else None


def resume_command(session_id: str) -> str:
    return f"/resume {session_id}"


@dataclass(frozen=True)
class Refusal:
    message: str


def switch_refusal(screen: Screen) -> Refusal | None:
    """Why typing into the client now could lose or misdirect something, if it could."""
    if screen.agent_view:
        return Refusal(
            "Claude is showing its session list. Press Esc in Claude, then select the row again."
        )
    if screen.composer is None:
        return Refusal(
            "Claude is showing a question or a dialog. Answer it, then select the row again."
        )
    if screen.working:
        return Refusal("Claude is still responding. Select the row again once it finishes.")
    if screen.draft:
        return Refusal(
            "Claude's prompt has unsent text. Send or clear it, then select the row again."
        )
    return None


@dataclass(frozen=True)
class SwitchPlan:
    """How to put a host pane's client on one conversation."""

    target: Session
    # A running background session must be stopped before an attached client can resume it.
    stop_first: bool
    # The conversation the switch stops in its place, to keep listing it.
    displaces: Session | None


def plan_switch(
    host: Host,
    target_id: str,
    sessions: list[Session],
    memory: HostMemory,
    active: str | None,
) -> SwitchPlan | Refusal:
    by_id = {session.session_id: session for session in sessions}
    target = by_id.get(target_id) or memory.displaced.get(target_id)
    if target is None:
        return Refusal("That Claude session has ended, so there is nothing to switch to.")
    if host.interactive is not None:
        # `/resume` here moves the client's own conversation to the background, still running.
        return SwitchPlan(target=target, stop_first=False, displaces=None)
    # An attached client can only resume a session that is not running, and resuming replaces
    # the conversation on screen. Both must be idle, so no running turn is cut short.
    current = by_id.get(active) if active else None
    if current is None:
        return Refusal(
            "Cannot tell which Claude session is on screen. Switch in Claude with ←, or attach "
            f"with `claude attach {target.short_id}`."
        )
    if current.is_working():
        return Refusal(
            f"“{current.name or current.short_id}” is still working. Select the row again once "
            "it finishes."
        )
    running = target_id in by_id
    if running and target.is_working():
        return Refusal(
            f"“{target.name or target.short_id}” is still working, and Claude can only bring it "
            "here once it stops. Open it from Claude's session list with ← instead."
        )
    return SwitchPlan(target=target, stop_first=running, displaces=current)


class Cli:
    """The external commands the service runs. Tests substitute their own."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, args: list[str], timeout: float = COMMAND_TIMEOUT) -> str:
        try:
            result = subprocess.run(
                args,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SessionsError(f"{args[0]}: {error}") from error
        if result.returncode != 0:
            detail = text(result.stderr) or text(result.stdout) or f"exit {result.returncode}"
            raise SessionsError(f"{' '.join(args[:3])} failed: {detail}")
        return result.stdout

    def sessions(self) -> list[Session]:
        return parse_sessions(self.run([self.settings.claude, "agents", "--json"]))

    def hooks_installed(self) -> bool:
        return has_hooks_plugin(self.run([self.settings.claude, "plugin", "list", "--json"]))

    def stop(self, session: Session) -> None:
        self.run([self.settings.claude, "stop", session.short_id])

    def panes(self) -> list[Pane]:
        return parse_panes(self.run([ROZI, "list-panes", "--format", "json"]))

    def screen_text(self, pane_id: int) -> str:
        data = response_data(
            self.run([ROZI, "capture-pane", "--target", str(pane_id), "--format", "json"]),
            "capture-pane",
        )
        return str(data.get("text", "")) if isinstance(data, dict) else ""

    def screen(self, pane_id: int) -> Screen:
        data = response_data(
            self.run(
                [
                    ROZI,
                    "capture-pane",
                    "--target",
                    str(pane_id),
                    "--render",
                    "spans",
                    "--format",
                    "json",
                ]
            ),
            "capture-pane",
        )
        rows = data.get("frame", {}).get("rows", []) if isinstance(data, dict) else []
        return read_screen(rows)

    def type_text(self, pane_id: int, value: str) -> None:
        self.run([ROZI, "send-text", "--target", str(pane_id), value])

    def press(self, pane_id: int, key: str) -> None:
        self.run([ROZI, "send-keys", "--target", str(pane_id), key])

    def notify(self, message: str, *, error: bool = False) -> None:
        args = [ROZI, "notify", "--title", "Claude Code"]
        if error:
            args += ["--level", "error"]
        try:
            self.run(args + ["--", message[:240]])
        except SessionsError:
            pass


def switch(
    cli: Cli,
    host: Host,
    row_id: str,
    memory: HostMemory,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Put the host pane's client on the conversation of row `row_id`, or say why not.

    Never types into a prompt that holds anything. Reads the typed command back before anything
    irreversible and again right before Enter, and reports success only once Claude shows the
    target. Returns whether it did."""
    target_id = memory.session_for_row(row_id)
    pane_id = host.pane.pane_id
    sessions = cli.sessions()
    screen = cli.screen(pane_id)
    listed = listed_sessions(host, sessions, "all") + list(memory.displaced.values())
    active = active_session(host, listed, screen.label, memory.last_target)
    if target_id == active:
        return True
    refusal = switch_refusal(screen)
    plan = refusal or plan_switch(host, target_id, sessions, memory, active)
    if isinstance(plan, Refusal):
        cli.notify(plan.message, error=True)
        return False

    command = resume_command(memory.origin(plan.target).session_id)
    cli.type_text(pane_id, command)
    # Claude redraws its prompt, and opens its slash-command hints, a moment after the keys land.
    for attempt in range(VERIFY_ATTEMPTS):
        typed = cli.screen(pane_id)
        if prompt_holds(typed, command):
            break
        if attempt + 1 < VERIFY_ATTEMPTS:
            sleep(VERIFY_INTERVAL)
    if not prompt_holds(typed, command):
        cli.notify(
            "Could not confirm the switch command in Claude's prompt, so it was not sent. "
            "Check the prompt before pressing Enter.",
            error=True,
        )
        return False
    if plan.stop_first:
        cli.stop(plan.target)
        # Stopping takes a moment, and the prompt is the user's the whole time. Read it again
        # right before Enter, so nothing typed meanwhile is submitted with the command.
        if not prompt_holds(cli.screen(pane_id), command):
            keep_stopped(memory, plan.target)
            cli.notify(
                "Claude's prompt changed while switching, so nothing was sent. "
                f"“{label_of(plan.target)}” was stopped and can be selected again.",
                error=True,
            )
            return False
    cli.press(pane_id, "Enter")
    for attempt in range(CONFIRM_ATTEMPTS):
        sleep(CONFIRM_INTERVAL)
        shown = cli.screen_text(pane_id)
        failure = switch_failure(shown, command)
        if failure is not None:
            if any(gone in failure for gone in GONE_FAILURES):
                # Nothing to resume, such as a conversation that never got a first prompt.
                memory.displaced.pop(plan.target.session_id, None)
            elif plan.stop_first:
                keep_stopped(memory, plan.target)
            cli.notify(f"Claude did not switch: {failure}", error=True)
            return False
        if switch_confirmed(plan, host, shown, cli.sessions(), memory.origin(plan.target)):
            memory.last_target = plan.target.session_id
            memory.displaced.pop(plan.target.session_id, None)
            if plan.displaces is not None:
                memory.displaced[plan.displaces.session_id] = plan.displaces
            return True
    # No answer either way. Claude may still switch, so nothing is recorded as on screen: the
    # active row keeps following what Claude itself shows.
    if plan.stop_first:
        keep_stopped(memory, plan.target)
    cli.notify(
        f"Could not confirm that Claude switched to “{label_of(plan.target)}”. "
        "Check the pane before selecting another row.",
        error=True,
    )
    return False


def prompt_holds(screen: Screen, command: str) -> bool:
    return screen.composer is not None and screen.composer.text == command


def label_of(session: Session) -> str:
    return session.name or session.short_id


def keep_stopped(memory: HostMemory, session: Session) -> None:
    """Keep listing a session this service stopped, so stopping it never loses its row."""
    memory.displaced[session.session_id] = session


def switch_confirmed(
    plan: SwitchPlan,
    host: Host,
    shown: str,
    sessions: list[Session],
    conversation: Session | None = None,
) -> bool:
    """Whether Claude now shows `plan.target`, from evidence that names the target.

    Claude draws the session it shows above its prompt. That label confirms the switch when it
    names the target and nothing else, and refutes it when it names only another conversation.
    Claude draws no label right after `/resume` from a client's own conversation, so there the
    conversation leaving the client confirms it: the command named the target by its full id,
    and Claude refused nothing. When the label names several, only the process can tell: a
    session resumed in place runs in the worker that showed the conversation it replaced.

    `conversation` is the conversation the target continues when it was first listed under
    another id. Resumed, Claude names and lists it as that conversation again.
    """
    target = plan.target
    conversation = conversation or target
    ids = {target.session_id, conversation.session_id}
    label = text_label(shown)
    names_target = label is not None and label in session_names(target) | session_names(
        conversation
    )
    others = [session for session in sessions if session.session_id not in ids]
    others += [session for session in (host.interactive, plan.displaces) if session is not None]
    names_other = label is not None and any(label in session_names(other) for other in others)
    if names_target and not names_other:
        return True
    if names_other and not names_target:
        return False
    if host.interactive is not None:
        own = host.interactive.session_id
        return label is None and not any(
            session.session_id == own and session.kind == "interactive" for session in sessions
        )
    if plan.displaces is None:
        return False
    running = next((s for s in sessions if s.session_id in ids), None)
    return running is not None and running.pid is not None and running.pid == plan.displaces.pid


def session_names(session: Session) -> set[str]:
    """Every way Claude may name `session` above its prompt."""
    return {name for name in (session.name, session.short_id, session.session_id) if name}


def write_line(stream: Any, value: object) -> None:
    stream.write(json.dumps(value, separators=(",", ":")) + "\n")
    stream.flush()


@dataclass
class Publisher:
    token: int
    process: subprocess.Popen[str]


class SessionsService:
    def __init__(self, settings: Settings, cli: Cli | None = None) -> None:
        self.settings = settings
        self.cli = cli or Cli(settings)
        self.messages: queue.Queue[tuple[object, ...]] = queue.Queue()
        self.publishers: dict[int, Publisher] = {}
        self.memory: dict[int, HostMemory] = {}
        self.hosts: dict[int, Host] = {}
        self.next_token = 1
        self.reported_error: str | None = None
        self.hooks_checked = False

    def start_publisher(self, pane_id: int) -> Publisher:
        token = self.next_token
        self.next_token += 1
        environment = os.environ.copy()
        # `ROZI_PANE` names the pane a publish stream belongs to.
        environment["ROZI_PANE"] = str(pane_id)
        process = subprocess.Popen(
            [ROZI, "publish"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
        )
        publisher = Publisher(token=token, process=process)
        self.publishers[pane_id] = publisher

        def read() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                row_id = activation(line)
                if row_id is not None:
                    self.messages.put(("activate", pane_id, token, row_id))
            process.wait()
            self.messages.put(("closed", pane_id, token))

        threading.Thread(target=read, name=f"publish-{pane_id}", daemon=True).start()
        return publisher

    def stop_publisher(self, pane_id: int) -> None:
        publisher = self.publishers.pop(pane_id, None)
        if publisher is None:
            return
        try:
            if publisher.process.stdin is not None:
                publisher.process.stdin.close()
        except OSError:
            pass
        try:
            publisher.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            publisher.process.kill()

    def publish(self, pane_id: int, rows: list[dict[str, object]]) -> None:
        memory = self.memory.setdefault(pane_id, HostMemory())
        publisher = self.publishers.get(pane_id)
        if publisher is None:
            if not rows:
                return
            publisher = self.start_publisher(pane_id)
            memory.published = None
        if memory.published == rows:
            return
        try:
            assert publisher.process.stdin is not None
            write_line(publisher.process.stdin, {"rows": rows})
            memory.published = rows
        except (BrokenPipeError, OSError):
            # The pane went away or the UI moved to another session. Try again next poll.
            self.stop_publisher(pane_id)

    def poll(self) -> None:
        sessions = self.cli.sessions()
        hosts = {host.pane.pane_id: host for host in classify(self.cli.panes(), sessions)}
        self.hosts = hosts
        for pane_id in set(self.publishers) - set(hosts):
            self.stop_publisher(pane_id)
        for pane_id in set(self.memory) - set(hosts):
            del self.memory[pane_id]
        for host in hosts.values():
            memory = self.memory.setdefault(host.pane.pane_id, HostMemory())
            label = None
            if host.interactive is None:
                try:
                    label = text_label(self.cli.screen_text(host.pane.pane_id))
                except SessionsError:
                    label = None
            follow_moved(memory, host, sessions, int(time.time() * 1000))
            rows = host_rows(host, sessions, self.settings.scope, memory, label)
            self.publish(host.pane.pane_id, rows)
        if hosts:
            self.suggest_hooks()

    def suggest_hooks(self) -> None:
        """Once a run, when Claude is in use, point out rozi's hook plugin if it is missing."""
        if self.hooks_checked or not self.settings.suggest_hooks:
            return
        self.hooks_checked = True
        try:
            installed = self.cli.hooks_installed()
        except SessionsError:
            # An older Claude without `plugin list --json`: nothing reliable to suggest.
            return
        if not installed:
            self.cli.notify(HOOKS_NOTICE)

    def activate(self, pane_id: int, row_id: str) -> None:
        host = self.hosts.get(pane_id)
        if host is None:
            return
        if not self.settings.switching:
            self.cli.notify("Switch to this session in Claude with ←, or with /resume.")
            return
        memory = self.memory.setdefault(pane_id, HostMemory())
        try:
            switch(self.cli, host, row_id, memory)
        except SessionsError as error:
            self.cli.notify(f"Could not switch Claude's session: {error}", error=True)
        # Show the result at once rather than at the next poll.
        memory.published = None

    def report(self, error: str | None) -> None:
        # One line per distinct failure: a missing `claude` would otherwise log every poll.
        if error is not None and error != self.reported_error:
            print(error, file=sys.stderr, flush=True)
        self.reported_error = error

    def handle(self, message: tuple[object, ...]) -> bool:
        """Act on one queued message; False once the service should stop."""
        kind = message[0]
        if kind == "stop":
            return False
        if kind == "closed":
            pane_id, token = message[1], message[2]
            current = self.publishers.get(pane_id)  # type: ignore[arg-type]
            if current is not None and current.token == token:
                self.publishers.pop(pane_id)  # type: ignore[arg-type]
        elif kind == "activate":
            pane_id, token, row_id = message[1], message[2], message[3]
            current = self.publishers.get(pane_id)  # type: ignore[arg-type]
            if current is not None and current.token == token:
                self.activate(pane_id, str(row_id))  # type: ignore[arg-type]
        return True

    def run(self) -> int:
        while True:
            try:
                self.poll()
                self.report(None)
            except SessionsError as error:
                self.report(str(error))
            deadline = time.monotonic() + self.settings.poll_seconds
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    message = self.messages.get(timeout=remaining)
                except queue.Empty:
                    break
                if not self.handle(message):
                    return 0

    def close(self) -> None:
        for pane_id in list(self.publishers):
            self.stop_publisher(pane_id)


def activation(line: str) -> str | None:
    """The row id in one line of a publish stream's output, if it is an activation."""
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return text(value.get("activate"))


def main() -> int:
    if os.environ.get("ROZI_EXTENSION") != EXTENSION_ID:
        print(f"{EXTENSION_ID} must be launched by Rozi", file=sys.stderr)
        return 2
    service = SessionsService(Settings.from_environment(dict(os.environ)))

    def stop(_signum: int, _frame: object) -> None:
        service.messages.put(("stop",))

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return service.run()
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
