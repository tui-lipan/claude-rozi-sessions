from __future__ import annotations

import importlib.util
import json
import os
import queue
import stat
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "bin" / "claude_sessions.py"


def load_script():
    spec = importlib.util.spec_from_file_location("claude_sessions_example", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cs = load_script()

RULE = "─" * 40


def session(
    sid: str,
    *,
    kind: str = "background",
    pid: int | None = None,
    state: str = "done",
    status: str | None = None,
    name: str | None = None,
    cwd: str = "/home/x/rozi",
    started: int = 0,
) -> dict[str, object]:
    """One entry as `claude agents --json` prints it."""
    value: dict[str, object] = {
        "pid": pid,
        "id": sid[:8],
        "cwd": cwd,
        "kind": kind,
        "startedAt": started,
        "sessionId": sid,
        # Claude reports a session busy while its turn runs.
        "status": status or ("busy" if state == "working" else "idle"),
    }
    if kind != "interactive":
        value["state"] = state
    if name:
        value["name"] = name
    return value


def sessions(*entries: dict[str, object]) -> list:
    return cs.parse_sessions(json.dumps(list(entries)))


def pane(pid: int | None = 500, program: str = "claude", cwd: str = "/home/x/rozi"):
    return cs.Pane(pane_id=1, cwd=cwd, program=program, agent=None, foreground_pid=pid)


def frame(*lines: object) -> list[list[dict[str, object]]]:
    """A styled capture: a string is one plain row, a list is the row's spans as given."""
    return [[{"text": line}] if isinstance(line, str) else list(line) for line in lines]


def prompt(typed: str = "", placeholder: str = 'Try "how does <filepath> work?"', label: str = ""):
    """The bottom of a Claude conversation screen, as captured in a rozi pane."""
    top = f"{RULE} {label} ─" if label else RULE
    spans: list[dict[str, object]] = [{"text": "❯\xa0" + typed}]
    if not typed and placeholder:
        spans.append({"text": placeholder, "dim": True})
    return frame(
        "● OK",
        top,
        spans,
        RULE,
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    )


def session_list(prompt_row: list[dict[str, object]] | None = None):
    """Claude's session list, with its empty prompt as Claude 2.1 draws it unless given."""
    empty = [
        {"fg": "#999999", "text": "❯"},
        {"text": " "},
        {"fg": "#999999", "text": "describe a task for a new session"},
    ]
    return frame(
        "Your conversation moved to the background — enter opens it · esc returns to it",
        "Needs input",
        " ✻ new session                               send a prompt to start",
        RULE,
        prompt_row or empty,
        RULE,
        "  ⏵⏵ auto mode · enter to return · space to reply · ctrl+x to delete · ? for shortcuts",
    )


class SessionTests(unittest.TestCase):
    def test_states_map_onto_rozis_vocabulary(self) -> None:
        def state(value: str, status: str = "idle", **extra: str) -> tuple[str, str | None]:
            return cs.Session.from_wire(
                {"sessionId": "s", "state": value, "status": status, **extra}
            ).row_state()

        self.assertEqual(state("working", "busy"), ("working", None))
        # Its turn over, though Claude has not marked the task finished: nothing runs.
        self.assertEqual(state("working"), ("idle", "Waiting"))
        self.assertEqual(state("done"), ("done", None))
        self.assertEqual(
            state("blocked", "waiting", waitingFor="permission prompt"),
            ("blocked", "Permission prompt"),
        )
        self.assertEqual(state("blocked", "waiting"), ("blocked", None))
        self.assertEqual(
            cs.Session.from_wire({"sessionId": "s", "status": "waiting"}).row_state(),
            ("blocked", None),
        )
        # A finished turn whose reply ends in a question shows no dialog: the turn is over.
        self.assertEqual(state("blocked"), ("done", "Awaiting your reply"))
        self.assertEqual(
            cs.Session.from_wire({"sessionId": "s", "state": "blocked"}).row_state(),
            ("done", "Awaiting your reply"),
        )
        self.assertEqual(state("stopped"), ("idle", "Stopped"))
        self.assertEqual(state("failed"), ("error", "Session failed"))
        # Waiting for its first prompt: Claude names it after its own ID until then.
        self.assertEqual(state("blocked", id="s", name="s"), ("idle", None))
        self.assertEqual(state("blocked", "waiting", id="s", name="s"), ("blocked", None))
        # An unknown word must not reach rozi, which would read it as a live run.
        self.assertEqual(state("pondering", "busy"), ("working", None))
        self.assertEqual(state("pondering"), ("idle", None))

    def test_a_session_waiting_for_its_first_prompt_is_a_new_idle_session(self) -> None:
        (fresh,) = sessions(session("b63210ff-a0f2", name="b63210ff", state="blocked"))
        self.assertTrue(fresh.untitled)
        self.assertIsNone(fresh.name)
        row = fresh.row(False)
        self.assertEqual((row["title"], row["status"], row.get("reason")),
                         ("New session", "idle", None))
        # Once titled, the same session reads as any other.
        (titled,) = sessions(session("b63210ff-a0f2", name="fix login", state="blocked"))
        self.assertEqual((titled.row(False)["title"], titled.row_state()),
                         ("fix login", ("done", "Awaiting your reply")))

    def test_a_row_names_its_conversation_and_where_it_works(self) -> None:
        (one,) = sessions(session("ec65255f-ce6d", name="remote install", cwd="/w/fix"))
        self.assertEqual(
            one.row(active=True),
            {
                "id": "ec65255f-ce6d",
                "title": "remote install",
                "status": "done",
                "active": True,
                "native_session": "ec65255f-ce6d",
                "cwd": "/w/fix",
            },
        )

    def test_failed_sessions_publish_error_for_every_failure_state(self) -> None:
        for state in ("failed", "error", "errored", "crashed"):
            with self.subTest(state=state):
                (failed,) = sessions(session("failed-session", state=state))
                row = failed.row(active=False)
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["reason"], "Session failed")
                self.assertFalse(failed.is_working())

    def test_malformed_entries_are_skipped_and_garbage_is_an_error(self) -> None:
        self.assertEqual(cs.parse_sessions('[{"pid": 1}, 7, {"id": "a"}]')[0].session_id, "a")
        with self.assertRaises(cs.SessionsError):
            cs.parse_sessions("not json")
        with self.assertRaises(cs.SessionsError):
            cs.parse_sessions('{"sessions": []}')

    def test_activation_lines_yield_the_row_id(self) -> None:
        self.assertEqual(cs.activation('{"activate":"abc"}\n'), "abc")
        self.assertIsNone(cs.activation('{"activate":""}'))
        self.assertIsNone(cs.activation("garbage"))
        self.assertIsNone(cs.activation('["activate"]'))


class GeneratedNameTests(unittest.TestCase):
    def own_row(self, name: str, title: str | None) -> dict[str, object]:
        listed = sessions(
            session("own", kind="interactive", pid=500, name=name),
            session("bg", pid=900, name="Other work"),
        )
        hosts = cs.classify(
            [replace(pane(), title=title)], listed, group_of=lambda pid: pid
        )
        rows = cs.host_rows(hosts[0], listed, "all", cs.HostMemory(), None)
        return next(row for row in rows if row["id"] == "own")

    def test_a_new_conversation_reads_as_new_rather_than_by_its_generated_name(self) -> None:
        self.assertEqual(self.own_row("rozi-c3", "Claude Code")["title"], cs.NEW_SESSION)
        self.assertEqual(self.own_row("rozi-c3", None)["title"], cs.NEW_SESSION)

    def test_a_generated_name_gives_way_to_the_title_claude_keeps_current(self) -> None:
        self.assertEqual(
            self.own_row("rozi-c3", "⠂ Activity sidebar naming")["title"],
            "Activity sidebar naming",
        )

    def test_a_chosen_name_stays_whatever_the_terminal_title_says(self) -> None:
        self.assertEqual(self.own_row("my refactor", "Something else")["title"], "my refactor")
        # Another directory's name, or a longer suffix, is not the generated shape.
        self.assertEqual(self.own_row("rozi-release", "Something else")["title"], "rozi-release")

    def test_a_generated_name_can_still_be_switched_to(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500, name="rozi-c3"))
        self.assertFalse(listed[0].untitled)


class ClassifyTests(unittest.TestCase):
    def test_a_client_running_its_own_conversation_is_matched_by_process_group(self) -> None:
        listed = sessions(
            session("own", kind="interactive", pid=731),
            session("bg", pid=900),
        )
        # Launched through a shim: the pane's group is led by the shim, not by Claude.
        hosts = cs.classify([pane(pid=730)], listed, group_of=lambda pid: 730 if pid == 731 else None)
        self.assertEqual(hosts[0].interactive.session_id, "own")
        # Launched directly: Claude leads its own group.
        hosts = cs.classify([pane(pid=731)], listed, group_of=lambda pid: None)
        self.assertEqual(hosts[0].interactive.session_id, "own")

    def test_an_attached_client_has_no_conversation_of_its_own(self) -> None:
        listed = sessions(session("bg", pid=900), session("other", kind="interactive", pid=42))
        (host,) = cs.classify([pane(pid=500)], listed, group_of=lambda pid: pid)
        self.assertIsNone(host.interactive)

    def test_panes_without_claude_or_a_process_group_are_left_alone(self) -> None:
        listed = sessions(session("bg", pid=900))
        self.assertEqual(cs.classify([pane(program="nvim")], listed), [])
        # Windows, or a remote attachment: no foreground process group to match.
        self.assertEqual(cs.classify([pane(pid=None)], listed), [])


class RowTests(unittest.TestCase):
    def test_the_clients_own_conversation_is_done_once_a_turn_ends_in_it(self) -> None:
        memory = cs.HostMemory()

        def own_row(status: str, sid: str = "own", reason: str | None = None):
            value = {**session(sid, kind="interactive", pid=500), "status": status}
            if reason:
                value["waitingFor"] = reason
            listed = sessions(value)
            (host,) = cs.classify([pane()], listed, group_of=lambda pid: pid)
            (row,) = cs.host_rows(host, listed, "all", memory, None)
            return row["id"], row["status"], row.get("reason")

        # Claude lists it busy, waiting, or idle only: idle is `done` once a turn has run.
        self.assertEqual(own_row("idle"), ("own", "idle", None))
        self.assertEqual(own_row("busy"), ("own", "working", None))
        self.assertEqual(
            own_row("waiting", reason="permission prompt"),
            ("own", "blocked", "Permission prompt"),
        )
        self.assertEqual(own_row("idle"), ("own", "done", None))
        self.assertEqual(own_row("busy"), ("own", "working", None))
        # `/clear` starts another conversation, in which no turn has run yet.
        self.assertEqual(own_row("idle", sid="cleared"), ("cleared", "idle", None))

    def test_rows_run_oldest_first_with_the_clients_own_conversation_active(self) -> None:
        listed = sessions(
            session("late", started=3),
            session("own", kind="interactive", pid=500, started=2),
            session("early", started=1),
        )
        (host,) = cs.classify([pane()], listed, group_of=lambda pid: pid)
        rows = cs.host_rows(host, listed, "all", cs.HostMemory(), None)
        # rozi names a pane's rows by position, so the own conversation takes its place in time.
        self.assertEqual([row["id"] for row in rows], ["early", "own", "late"])
        self.assertEqual([row["active"] for row in rows], [False, True, False])

    def test_an_attached_client_is_on_the_session_named_above_its_prompt(self) -> None:
        listed = sessions(session("aaa", name="fix login"), session("bbb", name="docs"))
        host = cs.Host(pane=pane(), interactive=None)
        rows = cs.host_rows(host, listed, "all", cs.HostMemory(), "docs")
        self.assertEqual([row["active"] for row in rows], [False, True])

    def test_an_ambiguous_name_falls_back_to_the_last_switch(self) -> None:
        listed = sessions(session("aaa", name="same"), session("bbb", name="same"))
        host = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory(last_target="aaa")
        rows = cs.host_rows(host, listed, "all", memory, "same")
        self.assertEqual([row["active"] for row in rows], [True, False])
        # A switch target that has since ended marks nothing.
        memory.last_target = "gone"
        rows = cs.host_rows(host, listed, "all", memory, "same")
        self.assertEqual([row["active"] for row in rows], [False, False])

    def test_a_running_turn_tells_sessions_sharing_the_label_apart(self) -> None:
        idle = session("aaa", name="same")
        busy = session("bbb", name="same", state="working", status="busy")
        host = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory(working=True)
        rows = cs.host_rows(host, sessions(idle, busy), "all", memory, "same")
        self.assertEqual([row["active"] for row in rows], [False, True])
        # Its turn ended and the screen stopped saying: both sessions are idle now, and the one
        # last told apart stays on screen.
        memory.working = False
        rows = cs.host_rows(host, sessions(idle, session("bbb", name="same")), "all", memory, "same")
        self.assertEqual([row["active"] for row in rows], [False, True])
        # The other one running in the background while the client's screen sits idle.
        fresh = cs.HostMemory(working=False)
        other = session("aaa", name="same", state="working", status="busy")
        rows = cs.host_rows(host, sessions(other, session("bbb", name="same")), "all", fresh, "same")
        self.assertEqual([row["active"] for row in rows], [False, True])
        # Both running, or an unread screen, says nothing; the last switch still outranks it.
        both = sessions(other, busy)
        rows = cs.host_rows(host, both, "all", cs.HostMemory(working=True), "same")
        self.assertEqual([row["active"] for row in rows], [False, False])
        rows = cs.host_rows(host, sessions(idle, busy), "all", cs.HostMemory(), "same")
        self.assertEqual([row["active"] for row in rows], [False, False])
        switched = cs.HostMemory(working=True, last_target="aaa")
        rows = cs.host_rows(host, sessions(idle, busy), "all", switched, "same")
        self.assertEqual([row["active"] for row in rows], [True, False])

    def test_the_screen_says_whether_a_turn_runs(self) -> None:
        live = "✻ Channeling… (3m 6s · ↓ 9.3k tokens)\n" + f"{RULE} same ─\n❯\n{RULE}\n"
        self.assertTrue(cs.text_working(live + "  ⏵⏵ bypass permissions on · esc to interrupt"))
        self.assertTrue(cs.text_working(live + "  ⏵⏵ auto mode on (shift+tab to cycle)"))
        done = "✻ Cooked for 14s · done 12:15 PM\n" + f"{RULE} same ─\n❯\n{RULE}\n"
        self.assertFalse(cs.text_working(done + "  ⏵⏵ auto mode on (shift+tab to cycle)"))

    def test_a_displaced_conversation_stays_listed_until_it_runs_again(self) -> None:
        (old,) = sessions(session("old", name="earlier work"))
        host = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory(displaced={"old": old})
        rows = cs.host_rows(host, sessions(session("now", name="now")), "all", memory, "now")
        # A finished run stays finished, so rozi keeps its result and run time.
        self.assertEqual(
            [(row["id"], row["status"], row.get("reason")) for row in rows],
            [("now", "done", None), ("old", "done", "Stopped")],
        )
        cs.host_rows(host, sessions(session("old"), session("now")), "all", memory, None)
        self.assertEqual(memory.displaced, {})

    def test_a_displaced_conversation_without_a_finished_run_is_idle(self) -> None:
        (old,) = sessions(session("old", state="idle"))
        host = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory(displaced={"old": old})
        rows = cs.host_rows(host, sessions(session("now")), "all", memory, None)
        (stopped,) = [row for row in rows if row["id"] == "old"]
        self.assertEqual((stopped["status"], stopped["reason"]), ("idle", "Stopped"))

    def test_a_displaced_conversation_keeps_its_place(self) -> None:
        (old,) = sessions(session("old", started=1))
        host = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory(displaced={"old": old})
        rows = cs.host_rows(host, sessions(session("now", started=2)), "all", memory, None)
        self.assertEqual([row["id"] for row in rows], ["old", "now"])

    def test_cwd_scope_keeps_sessions_started_under_the_pane(self) -> None:
        listed = sessions(
            session("in", cwd="/home/x/rozi/.claude/worktrees/fix"),
            session("out", cwd="/home/x/tui-lipan"),
        )
        host = cs.Host(pane=pane(), interactive=None)
        rows = cs.host_rows(host, listed, "cwd", cs.HostMemory(), None)
        self.assertEqual([row["id"] for row in rows], ["in"])
        self.assertFalse(cs.is_under("/home/x/rozi-old", "/home/x/rozi"))


def host_pane(pane_id: int, pid: int | None = None, cwd: str = "/home/x/rozi"):
    return cs.Pane(pane_id=pane_id, cwd=cwd, program="claude", agent=None,
                   foreground_pid=pid if pid is not None else 500 + pane_id)


def key(pane_id: int, instance: str = "") -> tuple[str, int]:
    return instance, pane_id


def owned_rows(hosts, listed, *, shown=None, memories=None, owners=None, scope="all"):
    """Each pane's published row ids once the background sessions have their owners.

    Every pane here belongs to one session, so the panes go by their ids alone."""
    memories = {key(pane_id): memory for pane_id, memory in (memories or {}).items()}
    shown = {key(pane_id): session for pane_id, session in (shown or {}).items()}
    owners = {session: key(pane_id) for session, pane_id in (owners or {}).items()}
    owners = cs.assign_owners(hosts, listed, shown, memories, owners, scope)
    return {session: pane for session, (_, pane) in owners.items()}, {
        host.pane.pane_id: [
            row["id"]
            for row in cs.host_rows(host, listed, scope,
                                    memories.get(host.pane.key, cs.HostMemory()), None, owners)
        ]
        for host in hosts
    }


class OwnershipTests(unittest.TestCase):
    """Every client could list every background session; each is listed in one pane only, or
    rozi shows it once per pane."""

    def test_a_background_session_is_listed_by_one_client(self) -> None:
        listed = sessions(
            session("bg", started=1),
            session("one", kind="interactive", pid=501, started=2),
            session("two", kind="interactive", pid=502, started=3),
            session("three", kind="interactive", pid=503, started=4),
        )
        hosts = cs.classify([host_pane(1), host_pane(2), host_pane(3)], listed,
                            group_of=lambda pid: pid)
        _, rows = owned_rows(hosts, listed)
        self.assertEqual(rows, {1: ["bg", "one"], 2: ["two"], 3: ["three"]})

    def test_the_client_attached_to_a_session_lists_it(self) -> None:
        listed = sessions(session("bg", name="docs"), session("other", name="other"),
                          session("one", kind="interactive", pid=501))
        hosts = cs.classify([host_pane(1), host_pane(2)], listed, group_of=lambda pid: pid)
        owners, rows = owned_rows(hosts, listed, shown={1: "one", 2: "bg"})
        self.assertEqual(owners, {"bg": 2, "other": 1})
        self.assertEqual(rows, {1: ["one", "other"], 2: ["bg"]})

    def test_a_conversation_sent_to_the_background_stays_with_its_client(self) -> None:
        listed = sessions(session("moved"), session("own2", kind="interactive", pid=502))
        hosts = cs.classify([host_pane(1), host_pane(2)], listed, group_of=lambda pid: pid)
        (origin,) = sessions(session("first"))
        memories = {2: cs.HostMemory(moved={"moved": origin})}
        owners, _ = owned_rows(hosts, listed, memories=memories)
        self.assertEqual(owners["moved"], 2)

    def test_a_session_stays_with_its_pane_after_the_client_moves_on(self) -> None:
        listed = sessions(session("bg"), session("one", kind="interactive", pid=501))
        hosts = cs.classify([host_pane(1), host_pane(2)], listed, group_of=lambda pid: pid)
        owners, _ = owned_rows(hosts, listed, owners={"bg": 2})
        self.assertEqual(owners["bg"], 2)
        # Its pane closed: exactly one other takes it over.
        owners, _ = owned_rows(hosts[:1], listed, owners={"bg": 3})
        self.assertEqual(owners["bg"], 1)

    def test_a_session_goes_to_the_pane_nearest_above_it(self) -> None:
        listed = sessions(session("deep", cwd="/home/x/rozi/tree"), session("far", cwd="/srv"))
        hosts = [cs.Host(host_pane(1, cwd="/home/x"), None),
                 cs.Host(host_pane(2, cwd="/home/x/rozi"), None)]
        owners, _ = owned_rows(hosts, listed)
        self.assertEqual((owners["deep"], owners["far"]), (2, 1))
        owners, _ = owned_rows(hosts, listed, scope="cwd")
        self.assertEqual(owners, {"deep": 2})

    def test_a_poll_publishes_each_background_session_once(self) -> None:
        listed = sessions(
            session("bg", started=1),
            session("one", kind="interactive", pid=501, started=2),
            session("two", kind="interactive", pid=502, started=3),
        )

        class PollCli(FakeCli):
            def panes(self):
                return [host_pane(1), host_pane(2)]

        service = cs.SessionsService(cs.Settings(), cli=PollCli(listed, []))
        published = {}
        service.publish = lambda pane, rows: published.__setitem__(
            pane, [row["id"] for row in rows])
        service.poll()
        self.assertEqual(published, {key(1): ["bg", "one"], key(2): ["two"]})

    def test_a_pane_is_known_by_its_session_as_well_as_its_id(self) -> None:
        wire = {"id": 3, "foreground_program": "claude", "on_screen": False,
                "reference": {"session_instance": "local", "pane_id": 3, "generation": 1}}
        parsed = cs.Pane.from_wire(wire)
        self.assertEqual((parsed.key, parsed.on_screen), (key(3, "local"), False))
        self.assertTrue(cs.Pane.from_wire({"id": 3}).on_screen)

    def test_the_same_pane_id_in_two_sessions_publishes_twice(self) -> None:
        listed = sessions(session("one", kind="interactive", pid=501, started=1),
                          session("bg", started=2))
        here = replace(host_pane(1), instance="remote-dev")
        away = replace(host_pane(1), instance="local-dev", on_screen=False)

        class PollCli(FakeCli):
            def panes(self):
                return [here, away]

        service = cs.SessionsService(cs.Settings(), cli=PollCli(listed, []))
        published = {}
        service.publish = lambda pane, rows: published.__setitem__(
            pane, [row["id"] for row in rows])
        service.poll()
        # Pane 1 of the session off screen is not pane 1 of the session on screen.
        self.assertEqual(set(published), {key(1, "remote-dev"), key(1, "local-dev")})

    def test_a_session_off_screen_keeps_its_rows_without_reading_its_screen(self) -> None:
        listed = sessions(session("bg", started=1), session("other", started=2))
        away = replace(host_pane(1), instance="local", on_screen=False)

        class OffScreenCli(FakeCli):
            def panes(self):
                return [away]

            def screen_text(self, pane_id):
                raise AssertionError("a session off screen has no screen to read")

        service = cs.SessionsService(cs.Settings(), cli=OffScreenCli(listed, []))
        # The client showed "bg" at the last look, while its session was on screen.
        service.labels[key(1, "local")] = "bg"
        published = {}
        service.publish = lambda pane, rows: published.__setitem__(pane, rows)
        service.poll()
        rows = published[key(1, "local")]
        self.assertEqual([row["id"] for row in rows], ["bg", "other"])
        self.assertEqual([row["id"] for row in rows if row["active"]], ["bg"])
        self.assertFalse(service.label_changed())

    def test_a_row_of_a_session_off_screen_does_not_switch(self) -> None:
        cli = FakeCli([], [])
        service = cs.SessionsService(cs.Settings(), cli=cli)
        away = replace(pane(), instance="local", on_screen=False)
        service.hosts[away.key] = cs.Host(pane=away, interactive=None)
        service.activate(away.key, "bg")
        # Typing would reach pane 1 of the session on screen, another session's pane.
        self.assertEqual(cli.calls, [])

    def test_a_conversation_resumed_inside_a_worker_stays_with_its_client(self) -> None:
        own = session("own", kind="interactive", pid=501)
        before = session("old-conversation", pid=900, name="Old topic", started=5)

        class PollCli(FakeCli):
            title = "✳ Old topic"

            def panes(self):
                return [host_pane(1), replace(host_pane(2), title=self.title)]

        cli = PollCli(sessions(own, before), [], after=showing("Old topic"))
        service = cs.SessionsService(cs.Settings(), cli=cli)
        published = {}
        service.publish = lambda pane_key, rows: published.__setitem__(
            pane_key, [(row["id"], row["title"], row["status"], row["active"]) for row in rows])
        service.poll()
        self.assertEqual(published[key(2)], [("old-conversation", "Old topic", "done", True)])

        # `/resume` typed into pane 2: the same worker, with only its session id changed, and
        # still the old conversation's name; the terminal title follows the resumed one. Pane 1 is
        # the lowest pane in the same directory.
        after = {**before, "sessionId": "resumed", "state": "working", "status": "busy"}
        cli.listed = sessions(own, after)
        cli.title = "✳ Resumed topic"
        cli.after = [f"● reply\n{RULE}\n❯\n{RULE}\n  esc to interrupt"]
        service.poll()
        self.assertEqual(published[key(1)], [("own", "", "idle", True)])
        self.assertEqual(
            published[key(2)],
            [("old-conversation", "Old topic", "done", False), ("resumed", "Resumed topic", "working", True)],
        )


class ScreenTests(unittest.TestCase):
    def test_a_placeholder_is_not_a_draft(self) -> None:
        screen = cs.read_screen(prompt())
        self.assertIsNotNone(screen.composer)
        self.assertFalse(screen.draft)
        self.assertIsNone(cs.switch_refusal(screen))

    def test_typed_text_is_a_draft_even_where_a_placeholder_would_be(self) -> None:
        screen = cs.read_screen(prompt(typed="Try typed"))
        self.assertTrue(screen.draft)
        self.assertIn("unsent text", cs.switch_refusal(screen).message)
        # A draft running onto a second line is still a draft.
        lines = frame(RULE, [{"text": "❯\xa0"}], [{"text": "  second line"}], RULE)
        self.assertTrue(cs.read_screen(lines).draft)

    def test_a_dialog_has_no_prompt_to_type_into(self) -> None:
        dialog = frame(
            "Do you want to make this edit?",
            "❯ 1. Yes",
            "  2. No",
            "Esc to cancel",
        )
        screen = cs.read_screen(dialog)
        self.assertIsNone(screen.composer)
        self.assertIn("dialog", cs.switch_refusal(screen).message)

    def test_claudes_session_list_refuses_a_switch_command(self) -> None:
        # Claude draws this placeholder grey like the `❯`, not dim.
        screen = cs.read_screen(session_list())
        self.assertTrue(screen.agent_view)
        self.assertFalse(screen.draft)
        # Claude 2.1 answers `/resume` there with "/resume isn't available in agent view".
        self.assertIn("session list", cs.switch_refusal(screen).message)

    def test_a_running_turn_does_not_stop_a_switch(self) -> None:
        # Claude holds a command sent mid-turn until the turn ends, so nothing is cut short.
        spinner = frame("✶ Pondering… (12s · esc to interrupt)") + prompt()
        self.assertIsNone(cs.switch_refusal(cs.read_screen(spinner)))

    def test_the_session_name_above_the_prompt_is_read(self) -> None:
        self.assertEqual(cs.read_screen(prompt(label="rozi-lab-bg-one")).label, "rozi-lab-bg-one")
        self.assertIsNone(cs.read_screen(prompt()).label)
        text = "● OK\n" + f"{RULE} fix login ─\n" + "❯\n" + RULE + "\n  footer"
        self.assertEqual(cs.text_label(text), "fix login")

    def test_only_a_refusal_below_the_latest_command_counts(self) -> None:
        command = "/resume 1db5f207-c117"
        old = (
            f"❯ {command}\n  ⎿  Session {command[8:]} was not found.\n"
            f"❯ {command}\n● the conversation"
        )
        self.assertIsNone(cs.switch_failure(old, command))
        new = f"❯ {command}\n  ⎿  “two” is running in the background (1db5f207)."
        self.assertIn("running in the background", cs.switch_failure(new, command))
        # Switched away: the command is no longer on screen at all.
        self.assertIsNone(cs.switch_failure("● TWO\n❯", command))


class FakeCli:
    """Records every command the switch runs, against scripted screens and sessions."""

    def __init__(self, listed, screens, after="", listed_after=None, repositories=None):
        self.listed = listed
        # The Git repository of each directory, as `git rev-parse --git-common-dir` answers.
        self.repositories = repositories or {}
        self.screens = list(screens)
        # What Claude shows, and lists, once Enter has been pressed.
        self.after = after if isinstance(after, list) else [after]
        self.listed_after = listed_after
        self.calls: list[tuple] = []

    def sessions(self):
        entered = ("press", "Enter") in self.calls
        return self.listed_after if entered and self.listed_after is not None else self.listed

    def screen(self, pane_id):
        # The last scripted screen stays up, as a real one does until something changes it.
        self.calls.append(("screen",))
        frame_rows = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return cs.read_screen(frame_rows)

    def screen_text(self, pane_id):
        return self.after.pop(0) if len(self.after) > 1 else self.after[0]

    def repository(self, path):
        return self.repositories.get(path)

    def type_text(self, pane_id, value):
        self.calls.append(("type", value))

    def press(self, pane_id, key):
        self.calls.append(("press", key))

    def stop(self, session):
        self.calls.append(("stop", session.short_id))

    def notify(self, message, *, error=False):
        self.calls.append(("notify", message))


def actions(cli: FakeCli) -> list[tuple]:
    return [call for call in cli.calls if call[0] != "screen"]


def showing(label: str) -> str:
    """A plain-text capture of Claude attached to the session named `label`."""
    return f"● reply\n{RULE} {label} ─\n❯\n{RULE}\n  footer"


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = cs.HostMemory()

    def run_switch(self, cli, host, target):
        return cs.switch(cli, host, target, self.memory, sleep=lambda _: None)

    def test_a_client_with_its_own_conversation_resumes_without_stopping_anything(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id", pid=9))
        host = cs.Host(pane=pane(), interactive=listed[0])
        # An unnamed session is labelled with its short id, as Claude draws it.
        cli = FakeCli(
            listed,
            [prompt(), prompt(typed="/resume target-id")],
            after=["● reply\n❯", showing("target-i")],
            listed_after=sessions(session("forked", pid=8), session("target-id", pid=9)),
        )
        self.assertTrue(self.run_switch(cli, host, "target-id"))
        self.assertEqual(
            actions(cli), [("type", "/resume target-id"), ("press", "Enter")]
        )
        self.assertEqual(self.memory.last_target, "target-id")
        self.assertEqual(self.memory.displaced, {})

    def test_an_attached_client_stops_the_target_then_resumes_it_in_place(self) -> None:
        listed = sessions(session("shown-id", name="shown"), session("target-id", name="target"))
        host = cs.Host(pane=pane(), interactive=None)
        cli = FakeCli(
            listed,
            [prompt(label="shown"), prompt(typed="/resume target-id", label="shown")],
            after=[showing("shown"), showing("target")],
        )
        self.assertTrue(self.run_switch(cli, host, "target-id"))
        # The command is verified on screen before anything irreversible: stop, then Enter.
        self.assertEqual(
            actions(cli),
            [("type", "/resume target-id"), ("stop", "target-i"), ("press", "Enter")],
        )
        self.assertEqual(list(self.memory.displaced), ["shown-id"])

    def test_switching_back_to_a_displaced_conversation_needs_no_stop(self) -> None:
        listed = sessions(session("target-id", name="target"))
        (shown,) = sessions(session("shown-id", name="shown"))
        self.memory.displaced["shown-id"] = shown
        host = cs.Host(pane=pane(), interactive=None)
        cli = FakeCli(
            listed,
            [prompt(label="target"), prompt(typed="/resume shown-id", label="target")],
            after=showing("shown"),
        )
        self.assertTrue(self.run_switch(cli, host, "shown-id"))
        self.assertEqual(actions(cli), [("type", "/resume shown-id"), ("press", "Enter")])
        self.assertEqual(list(self.memory.displaced), ["target-id"])

    def test_a_draft_is_never_typed_over(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id"))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(listed, [prompt(typed="half-written thought")])
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertEqual([call[0] for call in actions(cli)], ["notify"])

    def test_text_that_does_not_read_back_exactly_is_not_submitted(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id"))
        host = cs.Host(pane=pane(), interactive=listed[0])
        # Someone typed at the same moment: the prompt holds more than the command.
        cli = FakeCli(listed, [prompt(), prompt(typed="x/resume target-id")])
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertNotIn(("press", "Enter"), actions(cli))
        self.assertIsNone(self.memory.last_target)

    def test_a_prompt_that_redraws_late_is_waited_for(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id"))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(
            listed,
            [prompt(), prompt(typed="/res"), prompt(typed="/resume target-id")],
            after=showing("target-i"),
            listed_after=sessions(session("target-id")),
        )
        self.assertTrue(self.run_switch(cli, host, "target-id"))
        self.assertEqual(actions(cli)[-1], ("press", "Enter"))

    def test_an_ended_session_is_reported_not_attempted(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(listed, [prompt()])
        self.assertFalse(self.run_switch(cli, host, "vanished"))
        self.assertEqual(len(actions(cli)), 1)
        self.assertIn("ended", actions(cli)[0][1])

    def test_a_working_conversation_on_screen_does_not_stop_a_switch(self) -> None:
        host = cs.Host(pane=pane(), interactive=None)
        shown_busy = sessions(
            session("shown-id", name="shown", state="working"), session("target-id", name="t")
        )
        cli = FakeCli(
            shown_busy,
            [prompt(label="shown"), prompt(typed="/resume target-id", label="shown")],
            after=showing("t"),
        )
        self.assertTrue(self.run_switch(cli, host, "target-id"))
        self.assertEqual(
            actions(cli),
            [("type", "/resume target-id"), ("stop", "target-i"), ("press", "Enter")],
        )

    def test_a_working_target_is_not_cut_short(self) -> None:
        host = cs.Host(pane=pane(), interactive=None)
        target_busy = sessions(
            session("shown-id", name="shown"), session("target-id", name="t", state="working")
        )
        cli = FakeCli(target_busy, [prompt(label="shown")])
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertNotIn("stop", [call[0] for call in actions(cli)])

    def test_an_unknown_screen_is_not_guessed_at(self) -> None:
        listed = sessions(session("a", name="same"), session("b", name="same"), session("t"))
        host = cs.Host(pane=pane(), interactive=None)
        cli = FakeCli(listed, [prompt(label="same")])
        self.assertFalse(self.run_switch(cli, host, "t"))
        self.assertIn("Cannot tell", actions(cli)[0][1])

    def test_a_refused_resume_is_reported_and_remembered_as_not_done(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id"))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(
            listed,
            [prompt(), prompt(typed="/resume target-id")],
            after="❯ /resume target-id\n  ⎿  Session target-id was not found.",
        )
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertIn("was not found", actions(cli)[-1][1])
        self.assertIsNone(self.memory.last_target)

    def test_a_conversation_claude_cannot_find_is_not_kept_listed(self) -> None:
        listed = sessions(session("shown-id", name="shown"), session("empty-id"))
        host = cs.Host(pane=pane(), interactive=None)
        cli = FakeCli(
            listed,
            [prompt(label="shown"), prompt(typed="/resume empty-id", label="shown")],
            after="❯ /resume empty-id\n  ⎿  Session empty-id was not found.",
        )
        self.assertFalse(self.run_switch(cli, host, "empty-id"))
        self.assertIn(("stop", "empty-id"), actions(cli))
        self.assertEqual(self.memory.displaced, {})

    def test_text_typed_while_the_target_stops_is_not_submitted(self) -> None:
        listed = sessions(session("shown-id", name="shown"), session("target-id", name="target"))
        host = cs.Host(pane=pane(), interactive=None)
        cli = FakeCli(
            listed,
            [
                prompt(label="shown"),
                prompt(typed="/resume target-id", label="shown"),
                # Read again after `claude stop`: someone added to the prompt meanwhile.
                prompt(typed="/resume target-id please", label="shown"),
            ],
        )
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertEqual(
            [call[0] for call in actions(cli)], ["type", "stop", "notify"]
        )
        self.assertIn("nothing was sent", actions(cli)[-1][1])
        # The target was stopped, so it stays listed to be selected again; nothing else moved.
        self.assertEqual(list(self.memory.displaced), ["target-id"])
        self.assertIsNone(self.memory.last_target)

    def test_silence_after_enter_is_not_success(self) -> None:
        listed = sessions(session("shown-id", name="shown"), session("target-id", name="target"))
        host = cs.Host(pane=pane(), interactive=None)
        # No refusal, but Claude keeps showing the original session.
        cli = FakeCli(
            listed,
            [prompt(label="shown"), prompt(typed="/resume target-id", label="shown")],
            after=showing("shown"),
        )
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertIn(("press", "Enter"), actions(cli))
        self.assertIn("Could not confirm", actions(cli)[-1][1])
        self.assertIsNone(self.memory.last_target)
        self.assertEqual(list(self.memory.displaced), ["target-id"])
        # The row on screen still follows Claude, not the attempted switch.
        rows = cs.host_rows(host, listed, "all", self.memory, "shown")
        self.assertEqual(
            [(row["id"], row["active"]) for row in rows if row["active"]], [("shown-id", True)]
        )

    def test_a_shared_name_needs_the_process_to_confirm(self) -> None:
        listed = sessions(
            session("shown-id", name="shown", pid=40),
            session("target-id", name="twin", pid=50),
            session("other-id", name="twin", pid=60),
        )
        host = cs.Host(pane=pane(), interactive=None)
        screens = [prompt(label="shown"), prompt(typed="/resume target-id", label="shown")]
        # Resumed in place: the target now runs in the worker that showed "shown".
        moved = sessions(session("target-id", name="twin", pid=40), session("other-id", name="twin", pid=60))
        cli = FakeCli(listed, screens, after=showing("twin"), listed_after=moved)
        self.assertTrue(self.run_switch(cli, host, "target-id"))

        # The same name on screen with the target running elsewhere proves nothing.
        self.memory = cs.HostMemory()
        stuck = sessions(session("shown-id", name="shown", pid=40), session("other-id", name="twin", pid=60))
        cli = FakeCli(listed, screens, after=showing("twin"), listed_after=stuck)
        self.assertFalse(self.run_switch(cli, host, "target-id"))

    def test_the_own_conversation_leaving_does_not_confirm_an_unnamed_target(self) -> None:
        listed = sessions(
            session("own", kind="interactive", pid=500),
            session("target-id", pid=9),
            session("other-id", pid=7),
        )
        host = cs.Host(pane=pane(), interactive=listed[0])
        screens = [prompt(), prompt(typed="/resume target-id")]
        # The client's own conversation left, the target's process is unchanged, and Claude
        # shows another session: that is not the target opening.
        moved_on = sessions(session("forked", pid=8), session("target-id", pid=9), session("other-id", pid=7))
        cli = FakeCli(listed, screens, after=showing("other-id"), listed_after=moved_on)
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertIsNone(self.memory.last_target)
        self.assertIn("Could not confirm", actions(cli)[-1][1])

    def test_the_own_conversation_leaving_without_a_label_confirms_the_switch(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id", pid=9))
        host = cs.Host(pane=pane(), interactive=listed[0])
        screens = [prompt(), prompt(typed="/resume target-id")]
        # Claude draws no label after `/resume` from its own conversation, which it moves to the
        # background under a new id.
        moved_on = sessions(session("forked", pid=8), session("target-id", pid=9))
        cli = FakeCli(listed, screens, after="● reply\n❯", listed_after=moved_on)
        self.assertTrue(self.run_switch(cli, host, "target-id"))
        self.assertEqual(self.memory.last_target, "target-id")

    def test_no_label_while_the_own_conversation_stays_confirms_nothing(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id", pid=9))
        host = cs.Host(pane=pane(), interactive=listed[0])
        screens = [prompt(), prompt(typed="/resume target-id")]
        cli = FakeCli(listed, screens, after="● reply\n❯")
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertIsNone(self.memory.last_target)
        self.assertIn("Could not confirm", actions(cli)[-1][1])

    def test_a_label_naming_another_session_refutes_process_evidence(self) -> None:
        listed = sessions(session("shown-id", name="shown", pid=40), session("target-id", pid=50))
        host = cs.Host(pane=pane(), interactive=None)
        screens = [prompt(label="shown"), prompt(typed="/resume target-id", label="shown")]
        # The target now runs in the replaced worker, but Claude still names "shown".
        moved = sessions(session("target-id", pid=40))
        cli = FakeCli(listed, screens, after=showing("shown"), listed_after=moved)
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertIsNone(self.memory.last_target)

    def test_a_session_of_another_repository_is_refused_before_anything_is_stopped(self) -> None:
        for interactive in (False, True):
            listed = sessions(
                session("own", kind="interactive", pid=500),
                session("shown-id", name="shown"),
                session("elsewhere-id", name="other work", cwd="/home/x/tui-lipan"),
            )
            host = cs.Host(pane=pane(), interactive=listed[0] if interactive else None)
            cli = FakeCli(
                listed,
                [prompt(label="shown")],
                repositories={"/home/x/rozi": "/home/x/rozi/.git", "/home/x/tui-lipan": "/home/x/tui-lipan/.git"},
            )
            self.assertFalse(self.run_switch(cli, host, "elsewhere-id"))
            self.assertEqual([call[0] for call in actions(cli)], ["notify"])
            self.assertIn("outside this Claude's repository", actions(cli)[0][1])
            self.assertIn("claude attach elsewher", actions(cli)[0][1])

    def test_a_worktree_of_the_same_repository_is_switched_to(self) -> None:
        worktree = "/home/x/rozi/.claude/worktrees/fix"
        listed = sessions(
            session("own", kind="interactive", pid=500), session("target-id", pid=9, cwd=worktree)
        )
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(
            listed,
            [prompt(), prompt(typed="/resume target-id")],
            after="● reply\n❯",
            listed_after=sessions(session("forked", pid=8), session("target-id", pid=9, cwd=worktree)),
            repositories={"/home/x/rozi": "/home/x/rozi/.git", worktree: "/home/x/rozi/.git"},
        )
        self.assertTrue(self.run_switch(cli, host, "target-id"))

    def test_same_repository_rules(self) -> None:
        repo = {"/r": "/r/.git", "/r/wt": "/r/.git", "/o": "/o/.git"}.get
        self.assertTrue(cs.same_repository("/r", "/r/", repo))
        self.assertTrue(cs.same_repository("/r", "/r/wt", repo))
        self.assertFalse(cs.same_repository("/r", "/o", repo))
        # Outside any repository only the very directory counts.
        self.assertFalse(cs.same_repository("/plain", "/other", repo))
        # An unknown directory does not block a switch.
        self.assertTrue(cs.same_repository(None, "/o", repo))

    def test_selecting_the_row_on_screen_does_nothing(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("bg"))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(listed, [prompt(typed="a draft is fine here")])
        self.assertTrue(self.run_switch(cli, host, "own"))
        self.assertEqual(actions(cli), [])


def queued(label: str, command: str) -> str:
    """A capture of Claude still responding, holding `command` until the turn ends."""
    return (
        f"● a long reply\n❯ {command}\n  ctrl+x ctrl+s to send now\n"
        f"{RULE} {label} ─\n❯ Press up to edit queued messages\n{RULE}\n  footer"
    )


class QueuedSwitchTests(unittest.TestCase):
    """A switch sent while Claude responds waits for the turn, followed across polls."""

    def setUp(self) -> None:
        self.memory = cs.HostMemory()
        self.listed = sessions(
            session("own", kind="interactive", pid=500, name="mine"), session("target-id", pid=9)
        )
        self.host = cs.Host(pane=pane(), interactive=self.listed[0])
        self.cli = FakeCli(
            self.listed,
            [prompt(label="mine"), prompt(typed="/resume target-id", label="mine")],
            after=queued("mine", "/resume target-id"),
        )
        self.assertFalse(cs.switch(self.cli, self.host, "target-id", self.memory, sleep=lambda _: None))

    def test_a_queued_switch_is_announced_and_followed(self) -> None:
        self.assertIsNotNone(self.memory.pending)
        self.assertIn("once it finishes responding", actions(self.cli)[-1][1])
        self.assertEqual(
            [call[0] for call in actions(self.cli)], ["type", "press", "notify"]
        )
        # Still responding a long while later: still followed, nothing reported.
        cs.follow_pending(self.cli, self.memory, now_ms=self.memory.pending.queued_at + 60_000)
        self.assertIsNotNone(self.memory.pending)
        self.assertEqual(len(actions(self.cli)), 3)
        # The turn ended and Claude switched: the own conversation left the client.
        self.cli.after = ["● reply\n❯"]
        self.cli.listed = sessions(session("forked", pid=8), session("target-id", pid=9))
        cs.follow_pending(self.cli, self.memory, now_ms=self.memory.pending.queued_at + 61_000)
        self.assertIsNone(self.memory.pending)
        self.assertEqual(self.memory.last_target, "target-id")

    def test_another_row_waits_for_the_queued_switch(self) -> None:
        cli = FakeCli(self.listed, [prompt(label="mine")])
        self.assertFalse(cs.switch(cli, self.host, "own", self.memory, sleep=lambda _: None))
        self.assertEqual([call[0] for call in actions(cli)], ["notify"])
        self.assertIn("once it finishes responding", actions(cli)[0][1])

    def test_a_queue_that_empties_without_a_switch_is_reported(self) -> None:
        start = self.memory.pending.queued_at
        # The user cleared the queue: Claude keeps its own conversation on screen.
        self.cli.after = [showing("mine")]
        cs.follow_pending(self.cli, self.memory, now_ms=start + 1_000)
        self.assertIsNotNone(self.memory.pending)
        cs.follow_pending(self.cli, self.memory, now_ms=start + 1_000 + cs.SETTLE_MS + 1)
        self.assertIsNone(self.memory.pending)
        self.assertIn("Could not confirm", actions(self.cli)[-1][1])
        self.assertIsNone(self.memory.last_target)

    def test_a_turn_longer_than_the_limit_is_no_longer_followed(self) -> None:
        start = self.memory.pending.queued_at
        cs.follow_pending(self.cli, self.memory, now_ms=start + cs.QUEUE_LIMIT_MS + 1)
        self.assertIsNone(self.memory.pending)
        self.assertIn("Could not confirm", actions(self.cli)[-1][1])


class QueuedResumeInPlaceTests(unittest.TestCase):
    def test_a_queued_switch_back_to_a_moved_conversation_survives_the_polls(self) -> None:
        before = sessions(
            session("early", started=1, name="early", pid=40),
            session("own", kind="interactive", pid=500, started=2, name="mine"),
        )
        (host,) = cs.classify([pane()], before, group_of=lambda pid: pid)
        attached = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory()
        cs.follow_moved(memory, host, before, now_ms=10)
        moved = sessions(
            session("early", started=1, name="early", pid=40, state="working"),
            session("job", started=5, name="mine (2)", pid=41),
        )
        cs.follow_moved(memory, attached, moved, now_ms=20)
        cli = FakeCli(
            moved,
            [prompt(label="early"), prompt(typed="/resume own", label="early")],
            after=queued("early", "/resume own"),
        )
        self.assertFalse(cs.switch(cli, attached, "own", memory, sleep=lambda _: None))
        self.assertIsNotNone(memory.pending)
        # Polls run while "early" finishes its turn; the stopped job stays listed under its row.
        stopped = sessions(session("early", started=1, name="early", pid=40, state="working"))
        cs.follow_moved(memory, attached, stopped, now_ms=30)
        rows = cs.host_rows(attached, stopped, "all", memory, "early")
        self.assertEqual([(row["id"], row["reason"] if "reason" in row else None) for row in rows], [("early", None), ("own", "Stopped")])
        # The turn ends and Claude resumes the conversation in the worker that showed "early".
        cli.after = [showing("mine")]
        cli.listed = sessions(session("own", started=1, name="mine", pid=40))
        cs.follow_pending(cli, memory, now_ms=memory.pending.queued_at + 40_000)
        self.assertIsNone(memory.pending)
        self.assertEqual(memory.last_target, "job")
        # "early" was streaming when the switch was sent; it is listed with its turn finished.
        rows = cs.host_rows(attached, cli.listed, "all", memory, "mine")
        self.assertEqual(
            [(row["id"], row["status"], row.get("reason")) for row in rows],
            [("early", "done", "Stopped"), ("own", "done", None)],
        )


class MovedConversationTests(unittest.TestCase):
    """`/resume` from a client's own conversation lists that conversation again under a new id."""

    def setUp(self) -> None:
        self.memory = cs.HostMemory()
        self.before = sessions(
            session("early", started=1, name="early"),
            session("own", kind="interactive", pid=500, started=2),
        )
        (self.host,) = cs.classify([pane()], self.before, group_of=lambda pid: pid)
        cs.follow_moved(self.memory, self.host, self.before, now_ms=10)
        self.after = sessions(
            session("early", started=1, name="early"),
            session("forked", started=5, state="blocked"),
        )
        self.attached = cs.Host(pane=pane(), interactive=None)

    def rows(self, listed, label=None):
        cs.follow_moved(self.memory, self.attached, listed, now_ms=20)
        return cs.host_rows(self.attached, listed, "all", self.memory, label)

    def test_the_moved_conversation_keeps_its_row_id_and_place(self) -> None:
        rows = self.rows(self.after, label="early")
        self.assertEqual([row["id"] for row in rows], ["early", "own"])
        # Claude keeps the transcript, and resumes it, under the first id.
        self.assertEqual([row["native_session"] for row in rows], ["early", "own"])
        # Its hooks now report under the new id, which is the one that speaks for the row.
        self.assertEqual([row.get("live_session") for row in rows], [None, "forked"])
        self.assertEqual([row["active"] for row in rows], [True, False])

    def test_selecting_the_moved_row_resumes_the_conversation_under_its_new_id(self) -> None:
        self.rows(self.after, label="early")
        self.assertEqual(self.memory.session_for_row("own"), "forked")
        cli = FakeCli(
            self.after,
            [prompt(label="early"), prompt(typed="/resume own", label="early")],
            after=[showing("early"), showing("forked")],
        )
        self.assertTrue(
            cs.switch(cli, self.attached, "own", self.memory, sleep=lambda _: None)
        )
        # The background job is stopped by its own id, and the conversation resumed by its first.
        self.assertEqual(
            actions(cli), [("type", "/resume own"), ("stop", "forked"), ("press", "Enter")]
        )
        # On screen now, it is still published under its first row id and in its first place.
        shown = sessions(session("forked", started=5))
        rows = cs.host_rows(self.attached, shown, "all", self.memory, None)
        self.assertEqual(
            [(row["id"], row["active"]) for row in rows], [("early", False), ("own", True)]
        )

    def test_switching_back_is_confirmed_by_the_conversations_own_name(self) -> None:
        # Claude lists the moved job under a name of its own. Resumed in place, the worker that
        # showed "early" lists the conversation by its first id and name again.
        before = sessions(
            session("early", started=1, name="early", pid=40),
            session("own", kind="interactive", pid=500, started=2, name="mine"),
        )
        (host,) = cs.classify([pane()], before, group_of=lambda pid: pid)
        memory = cs.HostMemory()
        cs.follow_moved(memory, host, before, now_ms=10)
        moved = sessions(
            session("early", started=1, name="early", pid=40),
            session("job", started=5, name="mine (2)", pid=41),
        )
        cs.follow_moved(memory, self.attached, moved, now_ms=20)
        cs.host_rows(self.attached, moved, "all", memory, "early")
        resumed = sessions(session("own", started=0, name="mine", pid=40))
        cli = FakeCli(
            moved,
            [prompt(label="early"), prompt(typed="/resume own", label="early")],
            after=showing("mine"),
            listed_after=resumed,
        )
        self.assertTrue(cs.switch(cli, self.attached, "own", memory, sleep=lambda _: None))
        self.assertEqual(list(memory.displaced), ["early"])
        cs.follow_moved(memory, self.attached, resumed, now_ms=30)
        rows = cs.host_rows(self.attached, resumed, "all", memory, "mine")
        # Resumed in the worker of "early", the conversation reports that worker's start, but both
        # rows keep the places they were first published in.
        self.assertEqual(
            [(row["id"], row["status"], row["active"]) for row in rows],
            [("early", "done", False), ("own", "done", True)],
        )

    def test_a_stopped_job_is_not_listed_beside_its_resumed_conversation(self) -> None:
        self.rows(self.after)
        (job,) = [s for s in self.after if s.session_id == "forked"]
        # A switch back that was not confirmed keeps the stopped job, while Claude already runs
        # the conversation again under its first id.
        self.memory.displaced["forked"] = job
        rows = self.rows(sessions(session("early", started=1), session("own", started=0)))
        self.assertEqual(sorted(row["id"] for row in rows), ["early", "own"])

    def test_two_new_sessions_in_the_directory_are_not_guessed_between(self) -> None:
        crowded = self.after + sessions(session("dispatched", started=6))
        rows = self.rows(crowded)
        self.assertEqual({row["id"] for row in rows}, {"early", "forked", "dispatched"})

    def test_a_session_appearing_after_the_window_is_not_the_moved_one(self) -> None:
        cs.follow_moved(self.memory, self.attached, self.before[:1], now_ms=20)
        cs.follow_moved(self.memory, self.attached, self.before[:1], now_ms=20 + cs.MOVE_WINDOW_MS + 1)
        rows = self.rows(self.after)
        self.assertEqual([row["id"] for row in rows], ["early", "forked"])

    def test_the_alias_is_forgotten_once_the_session_ends(self) -> None:
        self.rows(self.after)
        self.rows(sessions(session("early", started=1)))
        self.assertEqual(self.memory.moved, {})


class UnnamedConversationTests(unittest.TestCase):
    """A client that never sent a prompt is not listed. Opening Claude's session list sends its
    conversation to the background, untitled, and Claude names no session above the prompt when
    the client returns to it."""

    def setUp(self) -> None:
        self.memory = cs.HostMemory()
        self.host = cs.Host(pane=pane(), interactive=None)
        # An untitled session left over from before, already listed when the client started.
        self.stale = session("stale-id", state="blocked", name="stale-id")
        self.fresh = session("fresh-id", state="blocked", name="fresh-id", started=5)
        cs.follow_moved(self.memory, self.host, sessions(self.stale), now_ms=0)

    def rows(self, listed, now_ms=1_000):
        cs.follow_moved(self.memory, self.host, listed, now_ms)
        return [
            (row["id"], row["title"], row["active"])
            for row in cs.host_rows(self.host, listed, "all", self.memory, None)
        ]

    def test_the_client_shows_the_conversation_it_sent_to_the_background(self) -> None:
        listed = sessions(self.stale, self.fresh)
        self.memory.look(True, now_ms=900)
        # Browsing Claude's session list, nothing is on screen.
        self.assertEqual(
            self.rows(listed),
            [("stale-id", "New session", False), ("fresh-id", "New session", False)],
        )
        self.memory.look(False, now_ms=1_500)
        self.assertEqual(
            self.rows(listed, now_ms=2_000),
            [("stale-id", "New session", False), ("fresh-id", "New session", True)],
        )
        # Selecting its row is then a switch to what is already on screen: nothing is typed.
        cli = FakeCli(listed, [prompt()])
        self.assertTrue(cs.switch(cli, self.host, "fresh-id", self.memory, sleep=lambda _: None))
        self.assertEqual(actions(cli), [])

    def test_a_listed_client_sends_its_conversation_to_the_background_under_a_new_id(self) -> None:
        # Claude lists a new client's conversation as interactive under an automatic name, and
        # untitled under another id once ← sends it to the background.
        own = session("own-id", kind="interactive", pid=500, name="rozi-fc", started=5)
        listed = sessions(self.stale, own)
        (host,) = cs.classify([pane()], listed, group_of=lambda pid: pid)
        cs.follow_moved(self.memory, host, listed, now_ms=500)
        self.memory.look(True, now_ms=900)
        moved = sessions(self.stale, self.fresh)
        self.rows(moved)
        self.assertEqual(self.memory.unnamed, "fresh-id")
        self.memory.look(False, now_ms=1_500)
        # Without a prompt the first id has no transcript: the first prompt starts one under the
        # new id, the only one Claude resumes the conversation by. The row takes it.
        self.assertEqual(
            self.rows(moved, now_ms=2_000),
            [("stale-id", "New session", False), ("fresh-id", "New session", True)],
        )
        self.assertEqual(self.memory.moved, {})
        cli = FakeCli(moved, [prompt()])
        self.assertTrue(cs.switch(cli, self.host, "fresh-id", self.memory, sleep=lambda _: None))
        self.assertEqual(actions(cli), [])

    def test_a_session_appearing_long_after_the_session_list_is_not_claimed(self) -> None:
        self.memory.look(True, now_ms=0)
        self.memory.look(False, now_ms=1)
        rows = self.rows(sessions(self.stale, self.fresh), now_ms=cs.MOVE_WINDOW_MS + 1)
        self.assertEqual([active for _, _, active in rows], [False, False])

    def test_sessions_listed_at_the_first_poll_are_not_claimed(self) -> None:
        memory = cs.HostMemory()
        memory.look(True, now_ms=0)
        cs.follow_moved(memory, self.host, sessions(self.fresh), now_ms=0)
        self.assertIsNone(memory.unnamed)

    def test_a_session_with_no_prompt_is_never_stopped_to_resume_it(self) -> None:
        listed = sessions(self.stale, self.fresh)
        cli = FakeCli(listed, [prompt()])
        self.assertFalse(cs.switch(cli, self.host, "fresh-id", self.memory, sleep=lambda _: None))
        ((kind, message),) = actions(cli)
        self.assertEqual(kind, "notify")
        self.assertIn("no prompt yet", message)


class TitledByScreenTests(unittest.TestCase):
    """A conversation sent to the background before its first prompt stays listed by Claude under
    its id after that prompt. Only the terminal title of the client showing it names it. Polls as
    Claude Code 2.1 answered them in a live client."""

    def setUp(self) -> None:
        test = self

        class ScriptCli(FakeCli):
            def panes(self):
                return [replace(host_pane(1, pid=500), title=test.title)]

            def screen_text(self, pane_id):
                return test.screen

        self.cli = ScriptCli([], [])
        self.service = cs.SessionsService(cs.Settings(), cli=self.cli)
        self.published = {}
        self.service.publish = lambda pane, rows: self.published.__setitem__(pane, rows)
        self.list_screen = "\n".join(row_text(row) for row in session_list())
        self.conversation = "\n".join(row_text(row) for row in prompt())

    def poll(self, listed, title, screen):
        self.cli.listed, self.title, self.screen = sessions(*listed), title, screen
        self.service.poll()
        return [
            (row["id"], row["title"], row["status"], row["active"])
            for row in self.published.get(key(1), [])
        ]

    def test_the_first_prompt_titles_the_row_and_lets_it_be_switched_to(self) -> None:
        own = session("own-id", kind="interactive", pid=500, name="rozi-fc", started=1)
        fresh = session("fresh-id", pid=900, state="blocked", name="fresh-id", started=2)
        self.assertEqual(
            self.poll([own], "Claude Code", self.conversation),
            [("own-id", "New session", "idle", True)],
        )
        # ← opens Claude's session list, which titles the terminal after itself.
        self.assertEqual(
            self.poll([fresh], "1 awaiting input · claude agents", self.list_screen),
            [("fresh-id", "New session", "idle", False)],
        )
        # Enter returns to the conversation, which Claude calls the current session.
        self.assertEqual(
            self.poll([fresh], "current session", self.conversation),
            [("fresh-id", "New session", "idle", True)],
        )
        answered = replace_entry(fresh, state="done")
        self.assertEqual(
            self.poll([answered], "✳ Ok", self.conversation),
            [("fresh-id", "Ok", "done", True)],
        )
        # The title stays with the conversation once the client shows something else.
        self.assertEqual(
            self.poll([answered], "claude agents", self.list_screen),
            [("fresh-id", "Ok", "done", False)],
        )
        # From the list, Claude would refuse `/resume`, so nothing is stopped or typed.
        self.cli.screens = [session_list()]
        host, memory = self.service.hosts[key(1)], self.service.memory[key(1)]
        self.assertFalse(cs.switch(self.cli, host, "fresh-id", memory, sleep=lambda _: None))
        self.assertEqual([kind for kind, *_ in actions(self.cli)], ["notify"])
        # It has a transcript now, under its own id, so from another conversation its row
        # switches to it.
        other = session("other-id", pid=901, name="other work", started=3)
        self.assertEqual(
            self.poll([answered, other], "✳ other work", showing("other work")),
            [("fresh-id", "Ok", "done", False), ("other-id", "other work", "done", True)],
        )
        self.cli.calls.clear()
        self.cli.screens = [prompt(label="other work"),
                            prompt(typed="/resume fresh-id", label="other work")]
        cs.switch(self.cli, host, "fresh-id", memory, sleep=lambda _: None)
        self.assertEqual(
            actions(self.cli)[:3],
            [("type", "/resume fresh-id"), ("stop", "fresh-id"), ("press", "Enter")],
        )

    def test_titles_that_name_no_conversation_are_not_taken(self) -> None:
        for title in ("Claude Code", "current session", "claude agents",
                      "2 awaiting input · claude agents", "✳ ", None):
            self.assertIsNone(cs.conversation_title(title), title)
        self.assertEqual(cs.conversation_title("✳ Fix the login"), "Fix the login")


def replace_entry(entry: dict[str, object], **changes: object) -> dict[str, object]:
    return {**entry, **changes}


def row_text(row: list[dict[str, object]]) -> str:
    return "".join(str(span.get("text", "")) for span in row)


class ServiceTests(unittest.TestCase):
    def test_activations_from_a_replaced_stream_are_ignored(self) -> None:
        service = cs.SessionsService(cs.Settings(), cli=FakeCli([], []))
        service.hosts[1] = cs.Host(pane=pane(), interactive=None)
        seen = []
        service.activate = lambda pane_id, row: seen.append((pane_id, row))

        class Current:
            token = 2

        service.publishers[1] = Current()
        self.assertTrue(service.handle(("activate", 1, 1, "old-row")))
        self.assertTrue(service.handle(("activate", 1, 2, "row")))
        self.assertEqual(seen, [(1, "row")])
        self.assertFalse(service.handle(("stop",)))

    def test_the_stream_declares_its_rows_agents(self) -> None:
        service = cs.SessionsService(cs.Settings(), cli=FakeCli([], []))
        with mock.patch.object(cs.subprocess, "Popen") as popen:
            popen.return_value.stdout = iter(())
            service.start_publisher(key(7, "inst"))
        self.assertEqual(popen.call_args.args[0][1:], ["publish", "--kind", "agent"])
        # The stream names its session, so its rows stay on that session's pane 7 whichever
        # session is on screen.
        environment = popen.call_args.kwargs["env"]
        self.assertEqual((environment["ROZI_PANE"], environment["ROZI_SESSION_INSTANCE"]),
                         ("7", "inst"))

    def test_a_new_label_on_an_attached_client_is_noticed(self) -> None:
        class LabelCli:
            label = "first"

            def screen_text(self, pane_id: int) -> str:
                return showing(self.label)

        cli = LabelCli()
        service = cs.SessionsService(cs.Settings(), cli=cli)
        service.hosts[1] = cs.Host(pane=pane(), interactive=None)
        self.assertFalse(service.label_changed())
        self.assertFalse(service.label_changed())
        cli.label = "second"
        self.assertTrue(service.label_changed())
        self.assertFalse(service.label_changed())

    def test_opening_claudes_session_list_is_noticed(self) -> None:
        class ListCli:
            text = showing("first")

            def screen_text(self, pane_id: int) -> str:
                return self.text

        cli = ListCli()
        service = cs.SessionsService(cs.Settings(), cli=cli)
        service.hosts[1] = cs.Host(pane=pane(), interactive=None)
        service.memory[1] = cs.HostMemory()
        self.assertFalse(service.label_changed())
        cli.text = "Your conversation moved to the background — enter opens it\n❯"
        self.assertTrue(service.label_changed())
        self.assertTrue(service.memory[1].browsing)
        self.assertFalse(service.label_changed())

    def test_a_client_running_its_own_conversation_is_not_read_between_polls(self) -> None:
        cli = FakeCli([], [])
        service = cs.SessionsService(cs.Settings(), cli=cli)
        service.hosts[1] = cs.Host(pane=pane(), interactive=session("own"))
        self.assertFalse(service.label_changed())
        self.assertEqual(cli.calls, [])

    def test_switching_can_be_turned_off(self) -> None:
        cli = FakeCli([], [])
        service = cs.SessionsService(cs.Settings(switching=False), cli=cli)
        service.hosts[1] = cs.Host(pane=pane(), interactive=None)
        service.activate(1, "row")
        self.assertEqual([call[0] for call in cli.calls], ["notify"])


class ClientWatchTests(unittest.TestCase):
    """Polling again as soon as a Claude client starts or exits, from rozi's `foreground-changed`."""

    @staticmethod
    def event(program: str, previous: str = "") -> str:
        data = {"pane": "3", "program": program, "previous_program": previous, "pid": ""}
        return json.dumps({"event": "foreground-changed", "data": data})

    @staticmethod
    def agent(agent: str, state: str, previous: str) -> str:
        data = {"pane": "3", "agent": agent, "state": state, "previous_state": previous}
        return json.dumps({"event": "agent-state-changed", "data": data})

    def test_a_claude_agent_appearing_or_going_counts_but_not_its_turns(self) -> None:
        self.assertTrue(cs.client_changed(self.agent("claude", "idle", "")))
        self.assertTrue(cs.client_changed(self.agent("claude", "", "working")))
        self.assertFalse(cs.client_changed(self.agent("claude", "idle", "working")))
        self.assertFalse(cs.client_changed(self.agent("codex", "idle", "")))

    def test_only_a_claude_client_starting_or_exiting_counts(self) -> None:
        self.assertTrue(cs.client_changed(self.event("claude")))
        self.assertTrue(cs.client_changed(self.event("", previous="claude")))
        self.assertTrue(cs.client_changed(self.event("Claude-Code")))
        self.assertFalse(cs.client_changed(self.event("cargo", previous="ls")))
        self.assertFalse(cs.client_changed("not json"))
        self.assertFalse(cs.client_changed(json.dumps({"event": "foreground-changed"})))

    def test_a_client_change_polls_again_at_once(self) -> None:
        service = cs.SessionsService(cs.Settings(poll_seconds=60), cli=FakeCli([], []))
        polls = []
        service.poll = lambda: polls.append(time.monotonic())
        service.messages.put(("client",))
        service.messages.put(("stop",))
        started = time.monotonic()
        self.assertEqual(service.run(), 0)
        self.assertEqual(len(polls), 2, "the change ends the wait for the next poll")
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipIf(os.name == "nt", "the stand-in rozi is a POSIX shell script")
    def test_the_stream_queues_a_poll_and_a_refusal_leaves_the_poll_alone(self) -> None:
        def watch(script: str) -> tuple[list[tuple[object, ...]], str]:
            with tempfile.TemporaryDirectory() as directory:
                args = Path(directory) / "args"
                rozi = Path(directory) / "rozi"
                rozi.write_text(f'#!/bin/sh\necho "$@" > "{args}"\n{script}\n', encoding="utf-8")
                rozi.chmod(rozi.stat().st_mode | stat.S_IXUSR)
                messages: queue.Queue[tuple[object, ...]] = queue.Queue()
                with mock.patch.object(cs, "ROZI", str(rozi)):
                    cs.watch_clients(messages).join(timeout=5)
                return list(messages.queue), args.read_text().strip()

        streamed = "\n".join(
            f"echo '{line}'" for line in (self.event("cargo"), self.event("claude"))
        )
        queued, args = watch(streamed)
        self.assertEqual(args, "subscribe foreground-changed agent-state-changed")
        self.assertEqual(queued, [("client",)])
        refused, _ = watch("echo 'unknown event' >&2; exit 1")
        self.assertEqual(refused, [])


if __name__ == "__main__":
    unittest.main()

