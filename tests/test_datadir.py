import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from account_switcher import vault


class DataDirTests(unittest.TestCase):
    """The data folder is LimitSwitcher's own; the old AccountSwitcher one is moved over, once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        vault._moved.clear()
        self.addCleanup(vault._moved.clear)

    def where(self, platform):
        """(new, old, env, patches) for a platform, all under the temporary folder."""
        if platform == "win32":
            return self.base / "LimitSwitcher", self.base / "AccountSwitcher", {"LOCALAPPDATA": str(self.base)}
        return self.base / "limitswitcher", self.base / "account-switcher", {"XDG_DATA_HOME": str(self.base)}

    def call(self, platform, env):
        patched = {k: v for k, v in os.environ.items() if k != "ACCOUNT_SWITCHER_HOME"}
        patched.update(env)
        with mock.patch.dict(os.environ, patched, clear=True), mock.patch.object(sys, "platform", platform), \
                mock.patch.object(Path, "home", return_value=self.base):
            return vault.data_dir()

    def test_an_old_folder_is_moved_over_with_everything_in_it(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform):
                self.setUp()
                new, old, env = self.where(platform)
                old.mkdir(parents=True)
                (old / "meta.json").write_text('{"accounts": {}}')
                (old / "secrets").mkdir()
                (old / "secrets" / "a").write_text("login")
                self.assertEqual(self.call(platform, env), new)
                self.assertEqual((new / "meta.json").read_text(), '{"accounts": {}}')
                self.assertEqual((new / "secrets" / "a").read_text(), "login")
                self.assertFalse(old.exists())
                self.assertEqual(self.call(platform, env), new)  # and stays there

    def test_a_folder_the_user_renamed_already_is_left_alone(self):
        new, old, env = self.where("linux")
        new.mkdir()
        (new / "meta.json").write_text("mine")
        old.mkdir()
        (old / "meta.json").write_text("older")
        self.assertEqual(self.call("linux", env), new)
        self.assertEqual((new / "meta.json").read_text(), "mine")  # theirs wins, the old one is not touched
        self.assertTrue(old.exists())

    def test_a_first_run_uses_the_new_name_and_creates_nothing(self):
        new, old, env = self.where("win32")
        self.assertEqual(self.call("win32", env), new)
        self.assertFalse(new.exists() or old.exists())

    def test_if_it_cannot_be_moved_the_old_folder_keeps_being_used(self):
        new, old, env = self.where("linux")
        old.mkdir()
        (old / "meta.json").write_text("data")
        with mock.patch("os.replace", side_effect=PermissionError("in use")):
            self.assertEqual(self.call("linux", env), old)  # nothing is lost
        self.assertEqual((old / "meta.json").read_text(), "data")

    def test_an_explicit_home_is_used_as_it_is(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_SWITCHER_HOME": str(self.base / "x")}):
            self.assertEqual(vault.data_dir(), self.base / "x")

    def test_the_encryption_entropy_is_unchanged(self):
        self.assertEqual(vault.ENTROPY, b"AccountSwitcher.v1")  # saved logins are bound to it


if __name__ == "__main__":
    unittest.main()
