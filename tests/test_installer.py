"""The installer's platform-independent parts: updating the checkout safely. Uses throwaway git
repositories; nothing is installed."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_installer():
    spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts" / "installer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.origin, self.clone, self.other = base / "origin.git", base / "clone", base / "other"
        run("git", "init", "--quiet", "--bare", "-b", "main", str(self.origin), cwd=base)
        run("git", "clone", "--quiet", str(self.origin), str(self.other), cwd=base)
        self.commit(self.other, "a.txt", "one")
        run("git", "push", "--quiet", "origin", "HEAD:main", cwd=self.other)
        run("git", "clone", "--quiet", str(self.origin), str(self.clone), cwd=base)
        self.installer = load_installer()
        self.installer.ROOT = self.clone

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        run("git", "add", name, cwd=repo)
        run("git", "commit", "--quiet", "-m", f"{name}: {text}", cwd=repo)

    def publish(self, text):
        self.commit(self.other, "a.txt", text)
        run("git", "push", "--quiet", "origin", "HEAD:main", cwd=self.other)

    def test_up_to_date_then_fast_forward(self):
        self.assertEqual(self.installer.update_code("", False)[0], "current")
        self.publish("two")
        self.assertEqual(self.installer.update_code("", False)[0], "updated")
        self.assertEqual((self.clone / "a.txt").read_text(), "two")

    def test_local_edits_block_unless_forced(self):
        self.publish("two")
        (self.clone / "a.txt").write_text("my edit")
        self.assertEqual(self.installer.update_code("", False)[0], "blocked")
        self.assertEqual((self.clone / "a.txt").read_text(), "my edit")
        self.assertEqual(self.installer.update_code("", True)[0], "updated")
        self.assertEqual((self.clone / "a.txt").read_text(), "two")
        self.assertIn("installer backup", self.installer.git("stash", "list"))  # the edit is kept

    def test_local_commits_are_backed_up_when_forced(self):
        self.commit(self.clone, "mine.txt", "local")
        self.publish("two")
        self.assertEqual(self.installer.update_code("", False)[0], "blocked")
        self.assertEqual(self.installer.update_code("", True)[0], "updated")
        branches = self.installer.git("branch", "--list", "backup/*")
        self.assertIn("backup/update-", branches)
        self.assertFalse((self.clone / "mine.txt").exists())


if __name__ == "__main__":
    unittest.main()
