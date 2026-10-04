import unittest

import launcher_core


class TerminalAppTests(unittest.TestCase):
    def test_terminal_commands_are_not_identified_by_the_terminal_process(self):
        app = {"exec": "foot --app-id=claude-cli -e /home/u/.local/bin/claude"}
        self.assertIsNone(launcher_core.app_process_name(app))
        self.assertEqual(launcher_core.app_process_name({"exec": "foot"}), "foot")
        self.assertIn("claude-cli", launcher_core.app_window_classes({**app, "startup_class": "claude-cli"}))


class TerminalCommandTests(unittest.TestCase):
    def test_terminal_command_names_the_command_run_in_the_terminal(self):
        self.assertEqual(launcher_core.terminal_command({"exec": "foot --app-id=x -e /a/b/claude --flag"}), "claude")
        self.assertEqual(launcher_core.terminal_command({"exec": "kitty -- pi-with-llama"}), "pi-with-llama")
        self.assertIsNone(launcher_core.terminal_command({"exec": "foot"}))
        self.assertIsNone(launcher_core.terminal_command({"exec": "nautilus -e x"}))

    def test_child_process_names_of_unknown_pid_is_empty(self):
        self.assertEqual(launcher_core.child_process_names(0), set())
