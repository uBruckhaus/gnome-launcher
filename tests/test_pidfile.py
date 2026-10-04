import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pidfile

TOGGLE = Path(__file__).resolve().parent.parent / "launcher-toggle"


def run_toggle_function(snippet, env=None):
    """Source launcher-toggle (functions only) and run a bash snippet."""
    return subprocess.run(["bash", "-c", f'source "{TOGGLE}"; {snippet}'],
                          capture_output=True, text=True, env=env, timeout=10)


class PidFileTests(unittest.TestCase):
    def test_round_trip_in_private_runtime_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                pidfile.write()
                self.assertEqual(pidfile.read(), (os.getpid(), pidfile.process_starttime(os.getpid())))
                pidfile.remove_if_own()
                self.assertFalse((Path(tmp) / pidfile.PID_NAME).exists())

    def test_shared_runtime_dir_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o777)
            fallback = Path("/tmp") / f"simple-launcher-for-fools-{os.getuid()}"
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertEqual(pidfile.private_dir(), fallback)
            self.assertEqual(os.stat(fallback).st_mode & 0o777, 0o700)

    def test_symlinked_pid_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            target = Path(tmp) / "elsewhere"
            target.write_text("1:1\n")
            (Path(tmp) / pidfile.PID_NAME).symlink_to(target)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertIsNone(pidfile.read())
                with self.assertRaises(OSError):
                    pidfile.write()
            self.assertEqual(target.read_text(), "1:1\n")


class ToggleScriptTests(unittest.TestCase):
    def test_forged_record_paths_do_not_identify_other_process(self):
        # A record naming an unrelated process of ours, plus that process's own
        # interpreter and script, must not make the toggle signal it.
        victim = subprocess.Popen(["sleep", "30"])
        try:
            exe = os.path.realpath(f"/proc/{victim.pid}/exe")
            st = pidfile.process_starttime(victim.pid)
            result = run_toggle_function(
                f'is_launcher_process {victim.pid} {st} "{exe}" /usr/bin/sleep && echo MATCH || echo NO')
            self.assertEqual(result.stdout.strip(), "NO")
        finally:
            victim.kill()
            victim.wait()

    def test_pid_dir_owned_by_someone_else_is_refused(self):
        result = run_toggle_function('is_private_dir / && echo PRIVATE || echo REFUSED')
        self.assertEqual(result.stdout.strip(), "REFUSED")

    def test_world_writable_runtime_dir_falls_back_to_private_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o777)
            env = dict(os.environ, XDG_RUNTIME_DIR=tmp)
            result = run_toggle_function("resolve_pid_file", env=env)
            self.assertEqual(result.stdout.strip(),
                             f"/tmp/simple-launcher-for-fools-{os.getuid()}/{pidfile.PID_NAME}")


if __name__ == "__main__":
    unittest.main()
