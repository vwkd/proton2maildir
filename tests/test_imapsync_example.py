"""Runs examples/imapsync with its dovecot.conf and the commands in its README, as written.

Needs dovecot (>= 2.4), socat and imapsync in PATH. The copies go into a second local Dovecot set up like MXroute (mxroute-like.conf).
"""

from __future__ import annotations

import base64
import imaplib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from .support import (
    EXPECTED_FLAGS, EXPECTED_MAILBOXES, MESSAGES, RECEIVED, ROOT, SENT, convert, maildir_name, make_export,
    message_time, p2m,
)

EXAMPLE = ROOT / "examples" / "imapsync"
TOOLS = all(map(shutil.which, ["doveadm", "socat", "imapsync"]))


def readme_block(start: str, contains: str = "") -> str:
    """A bash code block from the example's README."""
    blocks = re.findall(r"```bash\n(.*?)```", (EXAMPLE / "README.md").read_text(), re.S)
    return next(code for code in blocks if code.startswith(start) and contains in code)


def readme_socat(maildir: Path, port: int) -> tuple[dict, list[str]]:
    """The README's socat command for maildir and port, as (environment, arguments). It runs from the repository root."""
    words = shlex.split(readme_block("PROTON_MAILDIR="))
    assert words.pop(0).startswith("PROTON_MAILDIR=")
    words[1] = words[1].replace("TCP-LISTEN:1143,", f"TCP-LISTEN:{port},")
    return {"PROTON_MAILDIR": str(maildir)}, words


def readme_mxroute_imapsync(port1: int, port2: int) -> list[str]:
    """The README's MXroute imapsync command, with host2 swapped for the local MXroute stand-in."""
    words = iter(shlex.split(readme_block("imapsync", "mxrouting").replace("\\\n", " ")))
    result = []
    for word in words:
        if word == "--ssl2":
            continue
        if word in ("--port1", "--host2", "--user2", "--passfile2", "--sslargs2"):
            next(words)
            result += {
                "--port1": [word, str(port1)],
                "--host2": [word, "127.0.0.1", "--port2", str(port2), "--nossl2", "--notls2"],
                "--user2": [word, "me", "--authmech2", "PREAUTH"],
                "--passfile2": [],
                "--sslargs2": [],
            }[word]
        else:
            result.append(word)
    return result


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(args: list[str], env: dict) -> subprocess.Popen:
    """Start socat from the repository root, and wait until it listens."""
    server = subprocess.Popen(args, cwd=ROOT, env={**os.environ, **env}, stderr=subprocess.DEVNULL, start_new_session=True)
    port = int(re.search(r"TCP-LISTEN:(\d+)", args[1]).group(1))
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return server
        except OSError:
            if time.monotonic() > deadline:
                stop_server(server)
                raise
            time.sleep(0.1)


def stop_server(server: subprocess.Popen) -> None:
    # socat forks a child (plus a Dovecot imap process) per connection, all in its process group.
    os.killpg(server.pid, signal.SIGTERM)
    server.wait(timeout=10)


def list_mailboxes(imap: imaplib.IMAP4) -> dict[str, str]:
    """Mailbox name (decoded from modified UTF-7) -> LIST attributes."""
    result = {}
    for line in imap.list()[1]:
        m = re.match(rb'\((.*?)\) "(.)" (.*)', line)
        name = m.group(3).decode()
        result[decode_mutf7(name[1:-1] if name.startswith('"') else name)] = m.group(1).decode()
    return result


def subscribed_mailboxes(imap: imaplib.IMAP4) -> set[str]:
    names = set()
    for line in imap.lsub()[1]:
        name = re.match(rb'\(.*?\) "." (.*)', line).group(1).decode()
        names.add(decode_mutf7(name[1:-1] if name.startswith('"') else name))
    return names


def decode_mutf7(name: str) -> str:
    def part(m):
        if not m.group(1):
            return "&"
        b64 = m.group(1).replace(",", "/")
        return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", part, name)


def encode_mutf7(name: str) -> str:
    out, buf = [], []
    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            buf.clear()
    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


SERVER_KEYWORDS = {"\\Recent", "$HasAttachment", "$HasNoAttachment"}  # set by the server itself


def fetch_full(imap: imaplib.IMAP4, mailbox: str):
    """Yield (flags without SERVER_KEYWORDS, INTERNALDATE, full message) for every message in mailbox."""
    typ, exists = imap.select('"' + encode_mutf7(mailbox) + '"', readonly=True)
    assert typ == "OK", mailbox
    count = int(exists[0])
    for start in range(1, count + 1, 50):
        typ, data = imap.fetch(f"{start}:{min(start + 49, count)}", "(FLAGS INTERNALDATE BODY.PEEK[])")
        for item in data:
            if isinstance(item, tuple):
                meta, body = item
                flags = set(re.search(rb"FLAGS \((.*?)\)", meta).group(1).decode().split()) - SERVER_KEYWORDS
                yield flags, re.search(rb'INTERNALDATE "(.*?)"', meta).group(1).decode(), body


def fetch_messages(imap: imaplib.IMAP4, mailbox: str) -> dict[str, tuple[set, str]]:
    """Subject -> (flags without SERVER_KEYWORDS, INTERNALDATE) for every message in mailbox."""
    return {re.search(rb"(?m)^Subject: (\S+)", body).group(1).decode(): (flags, internaldate)
            for flags, internaldate, body in fetch_full(imap, mailbox)}


def epoch(internaldate: str) -> int:
    return int(time.mktime(imaplib.Internaldate2tuple(f'INTERNALDATE "{internaldate}"'.encode())))


class ExampleCase(unittest.TestCase):
    """The README's setup (convert, then serve the Maildir with socat and dovecot.conf), and the MXroute stand-in to copy into."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p2m-"))
        self.maildir = self.tmp / "proton-maildir"
        self.port1, self.port2 = free_port(), free_port()
        target = ["socat", f"TCP-LISTEN:{self.port2},bind=127.0.0.1,reuseaddr,fork", "EXEC:doveadm -c tests/mxroute-like.conf exec imap"]
        self.servers = [start_server(target, {"TARGET_MAILDIR": str(self.tmp / "target")})]
        self.serving = False

    def tearDown(self):
        for server in self.servers:
            stop_server(server)
        shutil.rmtree(self.tmp)

    def convert(self, export: Path) -> None:
        result = convert(str(export), str(self.maildir))
        self.assertEqual(result.returncode, 0, result.stderr)
        if not self.serving:  # as in the README: convert first, then serve
            env, args = readme_socat(self.maildir, self.port1)
            self.servers.append(start_server(args, env))
            self.serving = True

    def imapsync(self, *, resyncflags: bool = False) -> str:
        args = readme_mxroute_imapsync(self.port1, self.port2)
        if resyncflags:
            args.remove("--noresyncflags")
        result = subprocess.run([*args, "--nolog", "--noreleasecheck", "--tmpdir", str(self.tmp)], cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr)
        return result.stdout

    def transferred(self, output: str) -> int:
        return int(re.search(r"^Messages transferred\s*:\s*(\d+)", output, re.M).group(1))

    def imap(self, port: int) -> imaplib.IMAP4:
        return imaplib.IMAP4("127.0.0.1", port)  # PREAUTH: no login


@unittest.skipUnless(TOOLS, "needs dovecot, socat, imapsync")
class ServeTest(ExampleCase):
    def test_served_like_bridge(self):
        self.convert(make_export(self.tmp / "export", "20261005_120000", MESSAGES))
        imap = self.imap(self.port1)
        try:
            mailboxes = list_mailboxes(imap)
            expected = {maildir_name(k): v for k, v in EXPECTED_MAILBOXES.items()}
            self.assertEqual({name for name, attrs in mailboxes.items() if "\\Noselect" not in attrs}, set(expected))
            self.assertIn("\\Junk", mailboxes["Spam"])
            self.assertIn("\\Sent", mailboxes["Sent"])
            for mailbox, names in expected.items():
                messages = fetch_messages(imap, mailbox)
                self.assertEqual(set(messages), names, mailbox)
                for name, (flags, internaldate) in messages.items():
                    if name in EXPECTED_FLAGS:
                        self.assertEqual(flags, EXPECTED_FLAGS[name], name)
                    self.assertEqual(epoch(internaldate), message_time(name), name)
        finally:
            imap.logout()


@unittest.skipUnless(TOOLS, "needs dovecot, socat, imapsync")
class ImapsyncTest(ExampleCase):
    """The README's MXroute command, then the later runs it describes."""

    def test_copy_and_copy_new_mail_later(self):
        self.convert(make_export(self.tmp / "export", "20261005_120000", MESSAGES))
        output = self.imapsync()
        self.assertEqual(self.transferred(output), sum(len(v) for v in EXPECTED_MAILBOXES.values()))

        target_names = {
            "INBOX": "INBOX", "Sent": "Sent", "Drafts": "Drafts", "Archive": "Archive", "Trash": "Trash",
            "Spam": "Junk", "Folders/Work": "INBOX.Work", "Folders/Work/2024": "INBOX.Work.2024",
            "Folders/Work/cur": "INBOX.Work.cur_", "Folders/Café ☕": "INBOX.Café ☕", "Folders/v1.2": "INBOX.v1_2",
            "Labels/Todo": "INBOX.Labels.Todo", "Labels/Follow up": "INBOX.Labels.Follow up",
            "Labels/Unused": "INBOX.Labels.Unused", "Unfiled": "INBOX.Unfiled",
        }
        imap = self.imap(self.port2)
        try:
            mailboxes = list_mailboxes(imap)
            for source, target in target_names.items():
                self.assertIn(target, mailboxes, f"{source} -> {target}; got {sorted(mailboxes)}")
                messages = fetch_messages(imap, target)
                self.assertEqual(set(messages), EXPECTED_MAILBOXES[source], target)
                for name, (flags, internaldate) in messages.items():
                    if name in EXPECTED_FLAGS:
                        self.assertEqual(flags, EXPECTED_FLAGS[name], f"{target}: {name}")
                    self.assertEqual(epoch(internaldate), message_time(name))
            self.assertNotIn("Spam", mailboxes)
            self.assertLessEqual(set(target_names.values()), subscribed_mailboxes(imap) | {"INBOX"})
        finally:
            imap.logout()

        # Same export again: nothing to copy.
        self.assertEqual(self.transferred(self.imapsync()), 0)

        # A later export: one new message, and one message read in Proton meanwhile.
        newer = dict(MESSAGES, new_mail=(["0", "5"], RECEIVED, 1, 0, 0))
        self.convert(make_export(self.tmp / "export", "20261006_120000", newer, unread_overrides={"inbox_unread": 0}))
        self.assertEqual(self.transferred(self.imapsync()), 1)
        imap = self.imap(self.port2)
        try:
            inbox = fetch_messages(imap, "INBOX")
            self.assertEqual(inbox["new_mail"][0], set())
            self.assertEqual(inbox["inbox_unread"][0], set())  # --noresyncflags kept the target's state
        finally:
            imap.logout()

        # Without --noresyncflags, imapsync sets Proton's current flags on messages already copied.
        self.assertEqual(self.transferred(self.imapsync(resyncflags=True)), 0)
        imap = self.imap(self.port2)
        try:
            self.assertEqual(fetch_messages(imap, "INBOX")["inbox_unread"][0], {"\\Seen"})
        finally:
            imap.logout()


# Where the README's MXroute command puts each Proton location (system label ID).
TARGET_LOCATIONS = {
    "0": "INBOX", "16": "INBOX", "8": "Drafts", "7": "Sent", "6": "Archive", "3": "Trash", "4": "Junk",
    "12": "INBOX.Scheduled", "9": "INBOX.Outbox",
}


def expected_targets(meta: dict, custom: dict[str, dict]) -> set[str]:
    """MXroute folders for a message, derived from its metadata and the README's mapping."""
    def path(label):  # "/" becomes MXroute's ".", and a "." inside a name becomes "_" (imapsync --fixslash2)
        parts = (label["Path"] or label["Name"]).split("/")
        return ".".join(p2m.dir_name(part).replace(".", "_") for part in parts if part)
    ids = meta["LabelIDs"] or []
    folders = [TARGET_LOCATIONS[i] for i in ids if i in TARGET_LOCATIONS]
    folders += ["INBOX." + path(custom[i]) for i in ids if i in custom and custom[i]["Type"] == 3]
    labels = ["INBOX.Labels." + path(custom[i]) for i in ids if i in custom and custom[i]["Type"] == 1]
    return set(folders or ["INBOX.Unfiled"]) | set(labels)


def bridge_flags(meta: dict) -> set[str]:
    """IMAP flags Proton Mail Bridge shows for a message (BuildFlagSetFromMessageMetadata)."""
    flags = set()
    if not meta["Unread"]:
        flags.add("\\Seen")
    if "10" in (meta["LabelIDs"] or []):
        flags.add("\\Flagged")
    if not meta["Flags"] & (RECEIVED | SENT):
        flags.add("\\Draft")
    if meta["IsReplied"] or meta["IsRepliedAll"]:
        flags.add("\\Answered")
    if meta["IsForwarded"]:
        flags |= {"$Forwarded", "Forwarded"}
    return flags


@unittest.skipUnless(os.environ.get("PROTON_EXPORT"), "set PROTON_EXPORT to a Proton Mail Export Tool export")
@unittest.skipUnless(TOOLS, "needs dovecot, socat, imapsync")
class RealExportTest(ExampleCase):
    """Copies a real export with the README's MXroute command and checks every message on arrival.

    Expectations come from the export's metadata alone: folders per the README's mapping, Bridge's flags, INTERNALDATE = Proton's time, and content identical to the .eml.
    """

    def test_every_message_arrives_intact(self):
        export = p2m.find_export(Path(os.environ["PROTON_EXPORT"]).expanduser().resolve())
        self.convert(export)

        custom = {label["ID"]: label for label in json.loads((export / "labels.json").read_text())["Payload"] or []
                  if not label["ID"].isdigit()}
        expected = {}
        for meta_path in export.glob("*.metadata.json"):
            meta = json.loads(meta_path.read_text())["Payload"]
            eml_path = export / f"{meta['ID']}.eml"
            if eml_path.is_file() and meta.get("WriterType", 0) == 0 and "40" not in (meta["LabelIDs"] or []):
                expected[meta["ID"]] = (meta, eml_path)

        self.imapsync()
        arrived: dict[str, set[str]] = {}
        imap = self.imap(self.port2)
        try:
            for mailbox, attrs in list_mailboxes(imap).items():
                if "\\Noselect" in attrs:
                    continue
                for flags, internaldate, body in fetch_full(imap, mailbox):
                    msg_id = re.search(rb"(?im)^X-Pm-Internal-Id:\s*(\S+)", body).group(1).decode()
                    meta, eml_path = expected[msg_id]
                    arrived.setdefault(msg_id, set()).add(mailbox)
                    where = f"{meta['Subject']!r} in {mailbox}"
                    self.assertEqual(flags, bridge_flags(meta), where)
                    self.assertEqual(epoch(internaldate), meta["Time"], where)
                    self.assertEqual(body, eml_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"), where)
        finally:
            imap.logout()
        self.assertEqual(arrived, {msg_id: expected_targets(meta, custom) for msg_id, (meta, _) in expected.items()})
        self.assertEqual(self.transferred(self.imapsync()), 0)


if __name__ == "__main__":
    unittest.main()
