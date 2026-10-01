from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


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
    status: str = "idle",
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
        "status": status,
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


class SessionTests(unittest.TestCase):
    def test_states_map_onto_rozis_vocabulary(self) -> None:
        def state(value: str, status: str = "idle") -> tuple[str, str | None]:
            return cs.Session.from_wire(
                {"sessionId": "s", "state": value, "status": status}
            ).row_state()

        self.assertEqual(state("working"), ("working", None))
        self.assertEqual(state("done"), ("done", None))
        self.assertEqual(state("needs_input"), ("blocked", None))
        self.assertEqual(state("stopped"), ("idle", "Stopped"))
        self.assertEqual(state("failed"), ("idle", "Session failed"))
        # An unknown word must not reach rozi, which would read it as a live run.
        self.assertEqual(state("pondering", "busy"), ("working", None))
        self.assertEqual(state("pondering"), ("idle", None))

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
    def test_a_client_alone_with_its_own_conversation_publishes_nothing(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500))
        (host,) = cs.classify([pane()], listed, group_of=lambda pid: pid)
        self.assertEqual(cs.host_rows(host, listed, "all", cs.HostMemory(), None), [])

    def test_the_clients_own_conversation_leads_and_is_the_active_row(self) -> None:
        listed = sessions(
            session("late", started=2),
            session("own", kind="interactive", pid=500),
            session("early", started=1),
        )
        (host,) = cs.classify([pane()], listed, group_of=lambda pid: pid)
        rows = cs.host_rows(host, listed, "all", cs.HostMemory(), None)
        self.assertEqual([row["id"] for row in rows], ["own", "early", "late"])
        self.assertEqual([row["active"] for row in rows], [True, False, False])

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

    def test_a_displaced_conversation_stays_listed_until_it_runs_again(self) -> None:
        (old,) = sessions(session("old", name="earlier work"))
        host = cs.Host(pane=pane(), interactive=None)
        memory = cs.HostMemory(displaced={"old": old})
        rows = cs.host_rows(host, sessions(session("now", name="now")), "all", memory, "now")
        self.assertEqual(
            [(row["id"], row["status"], row.get("reason")) for row in rows],
            [("now", "done", None), ("old", "idle", "Stopped")],
        )
        cs.host_rows(host, sessions(session("old"), session("now")), "all", memory, None)
        self.assertEqual(memory.displaced, {})

    def test_cwd_scope_keeps_sessions_started_under_the_pane(self) -> None:
        listed = sessions(
            session("in", cwd="/home/x/rozi/.claude/worktrees/fix"),
            session("out", cwd="/home/x/tui-lipan"),
        )
        host = cs.Host(pane=pane(), interactive=None)
        rows = cs.host_rows(host, listed, "cwd", cs.HostMemory(), None)
        self.assertEqual([row["id"] for row in rows], ["in"])
        self.assertFalse(cs.is_under("/home/x/rozi-old", "/home/x/rozi"))


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

    def test_claudes_session_list_is_never_typed_into(self) -> None:
        listing = frame(
            "Your conversation moved to the background — enter opens it",
            RULE,
            [{"text": "❯\xa0"}, {"text": "describe a task for a new session", "dim": True}],
            RULE,
        )
        screen = cs.read_screen(listing)
        self.assertTrue(screen.agent_view)
        self.assertIn("session list", cs.switch_refusal(screen).message)

    def test_a_running_turn_is_not_interrupted(self) -> None:
        streaming = prompt() + frame("  esc to interrupt")
        self.assertIn("still responding", cs.switch_refusal(cs.read_screen(streaming)).message)
        spinner = frame("✶ Pondering… (12s · esc to interrupt)") + prompt()
        self.assertTrue(cs.read_screen(spinner).working)

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

    def __init__(self, listed, screens, after=""):
        self.listed = listed
        self.screens = list(screens)
        self.after = after
        self.calls: list[tuple] = []

    def sessions(self):
        return self.listed

    def screen(self, pane_id):
        # The last scripted screen stays up, as a real one does until something changes it.
        self.calls.append(("screen",))
        frame_rows = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return cs.read_screen(frame_rows)

    def screen_text(self, pane_id):
        return self.after

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


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = cs.HostMemory()

    def run_switch(self, cli, host, target):
        return cs.switch(cli, host, target, self.memory, sleep=lambda _: None)

    def test_a_client_with_its_own_conversation_resumes_without_stopping_anything(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("target-id", pid=9))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(listed, [prompt(), prompt(typed="/resume target-id")])
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

    def test_working_sessions_are_not_cut_short(self) -> None:
        host = cs.Host(pane=pane(), interactive=None)
        shown_busy = sessions(
            session("shown-id", name="shown", state="working"), session("target-id")
        )
        cli = FakeCli(shown_busy, [prompt(label="shown")])
        self.assertFalse(self.run_switch(cli, host, "target-id"))
        self.assertIn("still working", actions(cli)[0][1])

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

    def test_selecting_the_row_on_screen_does_nothing(self) -> None:
        listed = sessions(session("own", kind="interactive", pid=500), session("bg"))
        host = cs.Host(pane=pane(), interactive=listed[0])
        cli = FakeCli(listed, [prompt(typed="a draft is fine here")])
        self.assertTrue(self.run_switch(cli, host, "own"))
        self.assertEqual(actions(cli), [])


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

    def test_switching_can_be_turned_off(self) -> None:
        cli = FakeCli([], [])
        service = cs.SessionsService(cs.Settings(switching=False), cli=cli)
        service.hosts[1] = cs.Host(pane=pane(), interactive=None)
        service.activate(1, "row")
        self.assertEqual([call[0] for call in cli.calls], ["notify"])


class SettingsTests(unittest.TestCase):
    def test_settings_fall_back_to_defaults(self) -> None:
        settings = cs.Settings.from_environment(
            {
                "ROZI_EXTENSION_CONFIG": json.dumps(
                    {"poll_seconds": 0, "scope": "bogus", "claude": " ", "switching": "yes"}
                )
            }
        )
        self.assertEqual(settings, cs.Settings(poll_seconds=0.5))
        self.assertEqual(cs.Settings.from_environment({}), cs.Settings())
        self.assertFalse(
            cs.Settings.from_environment(
                {"ROZI_EXTENSION_CONFIG": '{"switching": false}'}
            ).switching
        )


if __name__ == "__main__":
    unittest.main()
