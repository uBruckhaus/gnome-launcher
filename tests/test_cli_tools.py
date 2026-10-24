"""CLI agents from mise wrappers, shims and the PATH: listed only when installed, dated by the install."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cli_tools  # noqa: E402

WRAPPER = '#!/bin/bash\nexport MISE_MINIMUM_RELEASE_AGE=0\nmise use -g --quiet "{spec}" || exit 1\nexec mise x "{spec}" -- "{bin}" "$@"\n'


class CliToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin, self.mise = root / "bin", root / "mise"
        (self.mise / "installs").mkdir(parents=True)
        (self.mise / "shims").mkdir()
        self.bin.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def wrapper(self, name, spec):
        (self.bin / name).write_text(WRAPPER.format(spec=spec, bin=name))

    def ids(self, known=(), which=lambda _name: None):
        return {e["desktop_id"]: e for e in cli_tools.cli_entries(set(known), self.bin, self.mise, which)}

    def test_only_installed_tools_are_listed(self):
        self.wrapper("crush", "crush")
        self.wrapper("cursor-agent", "cursor-agent")
        (self.mise / "installs" / "cursor-agent").mkdir()
        entries = self.ids()
        self.assertEqual(set(entries), {"cli:cursor-agent"})
        entry = entries["cli:cursor-agent"]
        self.assertEqual(entry["name"], "Cursor Agent")
        self.assertEqual(entry["startup_class"], "io.github.ubruckhaus.Launcher.cursor-agent")
        self.assertIn(str(self.bin / "cursor-agent"), entry["argv"])
        self.assertTrue(entry["installed_at"])

    def test_prefixed_specs_and_agents_without_wrapper(self):
        self.wrapper("ghui", "npm:@kitlangton/ghui")
        (self.mise / "installs" / "npm-kitlangton-ghui").mkdir()
        (self.mise / "installs" / "codex").mkdir()
        (self.mise / "shims" / "codex").write_text("")
        self.assertEqual(set(self.ids()), {"cli:ghui", "cli:codex"})

    def test_skips_tools_other_entries_open_and_plain_commands(self):
        for name in ("claude", "gh"):
            self.wrapper(name, name)
            (self.mise / "installs" / name).mkdir()
        self.assertEqual(self.ids(known={"claude"}), {})

    def test_agents_installed_elsewhere_are_found_on_the_path(self):
        program = Path(self.tmp.name) / "npm" / "gemini"
        program.parent.mkdir()
        program.write_text("")
        entries = self.ids(which=lambda name: str(program) if name == "gemini" else None)
        self.assertEqual(set(entries), {"cli:gemini"})
        self.assertIn(str(program), entries["cli:gemini"]["argv"])
        self.assertEqual(self.ids(known={"gemini"}, which=lambda name: str(program) if name == "gemini" else None), {})

    def test_foreign_or_large_files_are_ignored(self):
        (self.bin / "evil").write_text('exec mise x "x" -- "other" "$@"\n')   # name mismatch
        (self.bin / "big").write_text("#" * 5000 + WRAPPER.format(spec="big", bin="big"))
        (self.mise / "installs" / "x").mkdir()
        (self.mise / "installs" / "big").mkdir()
        self.assertEqual(self.ids(), {})


if __name__ == "__main__":
    unittest.main()
