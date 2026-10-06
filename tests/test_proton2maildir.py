"""Tests for the proton2maildir module and command, on the synthetic export in support.py."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from .support import (
    EXPECTED_FLAGS, EXPECTED_MAILBOXES, MESSAGES, RECEIVED, convert, maildir_name, make_export, message_id,
    message_time, p2m,
)


class ReadExportTest(unittest.TestCase):
    def test_bridge_view(self):
        with tempfile.TemporaryDirectory(prefix="p2m-") as tmp:
            path = make_export(Path(tmp), "20261005_120000", MESSAGES)
            export = p2m.read_export(path)

        names = {message_id(name): name for name in MESSAGES}
        messages = {names[m.id]: m for m in export.messages}
        self.assertEqual(set(export.mailboxes), set(EXPECTED_MAILBOXES))
        for mailbox, expected in EXPECTED_MAILBOXES.items():
            self.assertEqual({name for name, m in messages.items() if mailbox in m.mailboxes}, expected, mailbox)
        for name, flags in EXPECTED_FLAGS.items():
            self.assertEqual(messages[name].flags, flags, name)
        for name, m in messages.items():
            self.assertEqual(m.time, message_time(name))
            self.assertEqual(m.eml.name, f"{message_id(name)}.eml")
        self.assertEqual(export.not_converted, [path / message_id("failed")])
        self.assertEqual(export.soft_deleted, 1)
        self.assertEqual(export.unknown_label_ids, {"99": 1, "lGone==": 1})


class WriteMaildirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p2m-"))
        self.export = make_export(self.tmp / "export", "20261005_120000", MESSAGES)
        self.maildir = self.tmp / "proton-maildir"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_layout_flags_and_dates(self):
        export_eml = self.export / f"{message_id('inbox_unread')}.eml"
        export_mtime = export_eml.stat().st_mtime
        result = convert(str(self.tmp / "export"), str(self.maildir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipped 1 soft-deleted", result.stdout)
        self.assertIn("99 (1)", result.stdout)
        self.assertIn("1 messages could not be decrypted", result.stderr)

        found = {}
        for cur in self.maildir.rglob("cur"):
            mailbox = str(cur.parent.relative_to(self.maildir))
            self.assertTrue((cur.parent / "new").is_dir() and (cur.parent / "tmp").is_dir(), mailbox)
            found[mailbox] = set()
            for f in cur.iterdir():
                name = next(n for n in MESSAGES if message_id(n) in f.name)
                found[mailbox].add(name)
                self.assertEqual(int(f.stat().st_mtime), message_time(name), f)
        self.assertEqual(found, {maildir_name(k): v for k, v in EXPECTED_MAILBOXES.items()})

        starred = next((self.maildir / "INBOX" / "cur").glob(f"*{message_id('inbox_starred')}*"))
        self.assertTrue(starred.name.endswith(":2,FPRSab"), starred.name)
        self.assertEqual((self.maildir / "INBOX" / "dovecot-keywords").read_text(), "0 $Forwarded\n1 Forwarded\n")
        self.assertFalse((self.maildir / "Spam" / "dovecot-keywords").exists())

        summary = json.loads((self.maildir / ".proton2maildir.json").read_text())
        self.assertEqual(summary["messages_in_export"], len(MESSAGES))
        self.assertEqual(summary["not_converted"], [str(self.export.resolve() / message_id("failed"))])
        self.assertEqual(export_eml.stat().st_mtime, export_mtime)  # the export is left untouched
        self.assertEqual(export_eml.stat().st_nlink, 1)

    def test_replaces_only_its_own_output(self):
        self.assertEqual(convert(str(self.tmp / "export"), str(self.maildir)).returncode, 0)
        make_export(self.tmp / "export", "20261006_120000", dict(MESSAGES, new_mail=(["0", "5"], RECEIVED, 1, 0, 0)))
        result = convert(str(self.tmp / "export"), str(self.maildir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mail_20261006_120000", result.stdout)
        inbox = [f.name for f in (self.maildir / "INBOX" / "cur").iterdir()]
        self.assertEqual(sum(message_id("new_mail") in n for n in inbox), 1)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["export", "proton-maildir"])

        other = self.tmp / "other"
        other.mkdir()
        (other / "keep").write_text("x")
        result = convert(str(self.tmp / "export"), str(other))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not written by proton2maildir", result.stderr)
        self.assertEqual([p.name for p in other.iterdir()], ["keep"])


if __name__ == "__main__":
    unittest.main()
