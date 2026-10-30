import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("migration", Path(__file__).resolve().parents[1] / "mods/herdr-migration/backend.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class FakeRuntime:
    def __init__(self, source):
        self.source = Path(source)
        self.env = {}
        self.document = {"spaces": [{"id": "existing", "name": "Keep my work", "folder": "/", "tabs": []}]}
        self.version = 1
        self.terminals = {}
        self.calls = []
        self.legacy = []
        self.crash_after_spawn = False
        self.conflict = False

    def cli(self, args):
        if args[1:3] == ["session", "list"]:
            return {"sessions": [{"default": True, "running": True, "session_dir": str(self.source.parent)}]}
        if args[-2:] == ["api", "snapshot"]:
            return {"result": {"snapshot": {"panes": self.legacy, "agents": self.legacy, "workspaces": []}}}
        if args[-2:] == ["agent", "list"]:
            return {"result": {"agents": self.legacy}}
        raise m.MigrationError("offline")

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "doc.get":
            return {"version": self.version, "value": copy.deepcopy(self.document)}
        if method == "terminal.list":
            return {"terminals": copy.deepcopy(list(self.terminals.values()))}
        if method == "terminal.get":
            return copy.deepcopy(self.terminals[params["terminal"]])
        if method == "terminal.run":
            address = "t" + str(len(self.terminals) + 1)
            terminal = {"address": address, "state": "live", "cwd": params["cwd"], "keep": params["keep"],
                        "labels": params["labels"], "status": None, "foreground": {"name": "zsh", "cwd": params["cwd"]}}
            self.terminals[address] = terminal
            if self.crash_after_spawn:
                self.crash_after_spawn = False
                raise m.MigrationError("connection interrupted after successful creation")
            return copy.deepcopy(terminal)
        if method == "doc.set":
            if self.conflict:
                self.conflict = False
                self.document["spaces"].append({"id": "concurrent", "name": "User created this", "tabs": []})
                self.version += 1
                raise m.MigrationError("Core conflict")
            if params["version"] != self.version:
                raise m.MigrationError("Core conflict")
            self.document = copy.deepcopy(params["value"])
            self.version += 1
            return {"version": self.version}
        raise AssertionError(method)

    def app(self, name, args):
        self.calls.append((name, args))
        return {"terminal": "resumed", "tab": "resume-tab", "space": args["space"]}

    def close(self):
        pass


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "app"
        self.home.mkdir()
        self.a, self.b = self.root / "api", self.root / "web"
        self.a.mkdir(); self.b.mkdir()
        self.source = self.root / "classic/session.json"
        self.source.parent.mkdir()
        self.raw = {"version": 1, "workspaces": [{"id": "w1", "custom_name": "Northstar", "identity_cwd": str(self.a),
                     "public_pane_numbers": {"1": 1, "2": 2}, "tabs": [{"custom_name": "Build", "panes": {
                         "1": {"cwd": str(self.a), "agent_session": {"agent": "claude", "kind": "id", "value": "exact-conversation"}},
                         "2": {"cwd": str(self.b)}}, "layout": {"Split": {"direction": "Horizontal", "ratio": 0.63,
                         "first": {"Pane": 1}, "second": {"Pane": 2}}}}]}]}
        m.write_json(self.source, self.raw)
        self.runtime = FakeRuntime(self.source)
        self.engine = m.Engine(self.home, self.root / "data", self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        plan = self.engine.scan(str(self.source))["plan"]
        state = self.engine.store.read()
        state["job"] = {"id": "run-test", "selected": ["w1"], "total": 2, "done": 0, "status": "running", "heartbeat": 0, "source_key": plan["source_key"]}
        self.engine.store.write(state)
        return plan, state

    def imported(self):
        plan, state = self.prepare()
        self.engine.perform(state, plan)
        return plan, state

    def test_complete_import_preserves_existing_and_mixed_folders(self):
        plan, state = self.imported()
        self.assertEqual(self.runtime.document["spaces"][0]["name"], "Keep my work")
        self.assertEqual(len(self.runtime.terminals), 2)
        self.assertEqual({t["cwd"] for t in self.runtime.terminals.values()}, {str(self.a), str(self.b)})
        root = self.runtime.document["spaces"][1]["tabs"][0]["root"]
        self.assertEqual(root["children"][0]["size"], {"weight": 0.63})
        self.assertEqual(root["children"][1]["size"], {"weight": 0.37})
        self.assertTrue(state["sources"][plan["source_key"]]["workspaces"]["w1"]["verified"])
        self.assertTrue((self.root / "data/runs/run-test/backup/complete.json").exists())
        self.assertFalse(any(method in ["terminal.kill", "terminal.send"] for method, _ in self.runtime.calls))

    def test_crash_between_spawn_and_journal_recovers_from_core_marker(self):
        plan, state = self.prepare()
        self.runtime.crash_after_spawn = True
        with self.assertRaises(m.MigrationError):
            self.engine.perform(state, plan)
        self.assertEqual(len(self.runtime.terminals), 1)
        state = self.engine.store.read()
        self.engine.perform(state, plan)
        self.assertEqual(len(self.runtime.terminals), 2)
        self.assertEqual(state["job"]["status"], "complete")

    def test_concurrent_app_edit_is_merged_after_version_conflict(self):
        plan, state = self.prepare()
        self.runtime.conflict = True
        self.engine.perform(state, plan)
        self.assertEqual({s["id"] for s in self.runtime.document["spaces"]} & {"existing", "concurrent"}, {"existing", "concurrent"})

    def test_scan_marks_already_imported_and_start_does_not_repeat(self):
        self.imported()
        plan = self.engine.scan(str(self.source))["plan"]
        self.assertTrue(plan["workspaces"][0]["imported"])
        with self.assertRaisesRegex(m.MigrationError, "Select at least"):
            self.engine.start(["w1"])
        self.assertEqual(len(self.runtime.terminals), 2)

    def test_backup_precedes_any_terminal_creation(self):
        plan, state = self.prepare()
        original = self.runtime.rpc
        def check(method, params):
            if method == "terminal.run":
                self.assertTrue((self.root / "data/runs/run-test/backup/complete.json").exists())
            return original(method, params)
        self.runtime.rpc = check
        self.engine.perform(state, plan)

    def test_missing_folder_is_visible_and_blocks_start(self):
        self.b.rmdir()
        plan = self.engine.scan(str(self.source))["plan"]
        self.assertEqual(plan["workspaces"][0]["missing"], [str(self.b)])
        with self.assertRaisesRegex(m.MigrationError, "no longer exist"):
            self.engine.start(["w1"])
        self.assertFalse(self.runtime.terminals)

    def test_source_structure_change_requires_new_preview(self):
        self.engine.scan(str(self.source))
        self.raw["workspaces"][0]["tabs"][0]["layout"]["Split"]["ratio"] = 0.5
        m.write_json(self.source, self.raw)
        with self.assertRaisesRegex(m.MigrationError, "changed since"):
            self.engine.start(["w1"])

    def test_nonstructural_focus_change_keeps_preview_valid(self):
        self.engine.scan(str(self.source))
        self.raw["selected"] = 4
        m.write_json(self.source, self.raw)
        with patch.object(m.subprocess, "Popen") as popen:
            popen.return_value.pid = 424242
            result = self.engine.start(["w1"])
        self.assertEqual(result["job"]["status"], "queued")

    def test_invalid_tree_missing_or_duplicate_pane_is_rejected(self):
        for leaf in [1, 99]:
            raw = copy.deepcopy(self.raw)
            raw["workspaces"][0]["tabs"][0]["layout"]["Split"]["second"] = {"Pane": leaf}
            with self.assertRaises(m.MigrationError):
                m.normalize_session(raw)

    def test_nan_and_invalid_split_proportions_are_rejected(self):
        for ratio in [float("nan"), float("inf"), 0, 1, -1, True]:
            raw = copy.deepcopy(self.raw)
            raw["workspaces"][0]["tabs"][0]["layout"]["Split"]["ratio"] = ratio
            with self.assertRaises(m.MigrationError):
                m.normalize_session(raw)

    def test_name_collision_is_reported_without_reusing_the_space(self):
        self.runtime.document["spaces"][0]["name"] = "Northstar"
        plan = self.engine.scan(str(self.source))["plan"]
        self.assertTrue(plan["workspaces"][0]["collision"])
        self.imported()
        self.assertEqual(len(self.runtime.document["spaces"]), 2)

    def test_paused_run_keeps_created_terminal_and_recovers_without_duplicates(self):
        plan, state = self.prepare()
        m.write_json(self.root / "data/runs/run-test/pause.request", {})
        with self.assertRaisesRegex(m.MigrationError, "paused"):
            self.engine.perform(state, plan)
        self.assertEqual(len(self.runtime.terminals), 1)
        (self.root / "data/runs/run-test/pause.request").unlink()
        self.engine.perform(self.engine.store.read(), plan)
        self.assertEqual(len(self.runtime.terminals), 2)

    def test_busy_destination_is_never_sent_a_resume(self):
        _, state = self.imported()
        first = next(iter(self.runtime.terminals.values()))
        first["foreground"]["name"] = "vim"
        with self.assertRaisesRegex(m.MigrationError, "busy"):
            self.engine.resume(first["address"])
        self.assertFalse(any(name == "tab.new" for name, _ in self.runtime.calls))

    def test_live_session_in_another_pane_blocks_duplicate_resume(self):
        self.imported()
        self.runtime.legacy = [{"pane_id": "w9:p9", "agent_session": {"value": "exact-conversation"}}]
        with self.assertRaisesRegex(m.MigrationError, "still open"):
            self.engine.resume("t1")

    def test_unavailable_classic_server_refuses_resume(self):
        self.imported()
        self.runtime.cli = lambda args: (_ for _ in ()).throw(m.MigrationError("server offline"))
        with self.assertRaisesRegex(m.MigrationError, "offline"):
            self.engine.resume("t1")

    def test_resume_uses_exact_argv_in_a_new_tab_and_prevents_second_launch(self):
        self.imported()
        with patch.object(m.shutil, "which", return_value="/bin/claude"):
            result = self.engine.resume("t1")
        self.assertEqual(result["terminal"], "resumed")
        call = next(args for name, args in self.runtime.calls if name == "tab.new")
        self.assertEqual(call["argv"], ["claude", "--resume", "exact-conversation"])
        with self.assertRaisesRegex(m.MigrationError, "already been resumed"):
            self.engine.resume("t1")

    def test_corrupt_history_is_not_overwritten(self):
        self.engine.store.state_file.write_text("{ broken")
        with self.assertRaises(m.MigrationError):
            self.engine.store.read()
        self.assertEqual(self.engine.store.state_file.read_text(), "{ broken")

    def test_active_operation_lock_prevents_second_writer(self):
        lock = self.engine.store.acquire()
        try:
            with self.assertRaisesRegex(m.MigrationError, "Another migration"):
                self.engine.store.acquire()
        finally:
            self.engine.store.release(lock)

    def test_stale_operation_lock_is_recovered_without_deleting_runs(self):
        lock = self.engine.store.root / "operation.lock"
        lock.mkdir()
        m.write_json(lock / "owner.json", {"pid": 999999999})
        acquired = self.engine.store.acquire()
        self.engine.store.release(acquired)
        self.assertFalse(lock.exists())

    def test_removed_destination_can_be_reimported_without_adopting_finished_shells(self):
        self.imported()
        self.runtime.document["spaces"] = self.runtime.document["spaces"][:1]
        for terminal in self.runtime.terminals.values():
            terminal["state"] = "finished"
        plan = self.engine.scan(str(self.source))["plan"]
        self.assertFalse(plan["workspaces"][0]["imported"])
        state = self.engine.store.read()
        state["job"]["status"] = "running"
        self.engine.perform(state, plan)
        self.assertEqual(len(self.runtime.terminals), 4)


if __name__ == "__main__":
    unittest.main()
