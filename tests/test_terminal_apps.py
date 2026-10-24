"""Terminal apps are known by the command they run, not the terminal."""
import unittest

import launcher_core


class TerminalAppTests(unittest.TestCase):
    def test_terminal_commands_are_not_identified_by_the_terminal_process(self):
        app = {"exec": "foot --app-id=claude-cli -e /home/u/.local/bin/claude"}
        self.assertIsNone(launcher_core.app_process_name(app))
        self.assertEqual(launcher_core.app_process_name({"exec": "foot"}), "foot")
        self.assertEqual(launcher_core.app_process_name({"exec": "ptyxis --new-window"}), "ptyxis")
        self.assertIsNone(launcher_core.app_process_name({"exec": "xdg-terminal-exec -e btop"}))


class TerminalCommandTests(unittest.TestCase):
    def test_terminal_command_names_the_command_run_in_the_terminal(self):
        self.assertEqual(launcher_core.terminal_command({"exec": "foot --app-id=x -e /a/b/claude --flag"}), "claude")
        self.assertEqual(launcher_core.terminal_command({"exec": "kitty -- pi-with-llama"}), "pi-with-llama")
        self.assertEqual(launcher_core.terminal_command(
            {"exec": "xdg-terminal-exec --app-id=io.github.ubruckhaus.Launcher.codex -e /home/u/.local/bin/codex"}),
            "codex")
        self.assertEqual(launcher_core.terminal_command({"exec": "ptyxis -- htop"}), "htop")
        self.assertIsNone(launcher_core.terminal_command({"exec": "foot"}))
        self.assertIsNone(launcher_core.terminal_command({"exec": "nautilus -e x"}))
