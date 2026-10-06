"""A synthetic Proton Mail Export Tool export, and what proton2maildir should make of it.

It mirrors what proton-mail-export writes (go-lib/internal/mail): versioned JSON with Go field names, booleans as 0/1, null for empty lists, one <ID>.eml per message, and a <ID>/ directory instead for messages it could not assemble.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "src" / "proton2maildir.py"
sys.path.insert(0, str(SCRIPT.parent))
import proton2maildir as p2m  # noqa: E402

RECEIVED, SENT = 1, 2
T0 = 1_700_000_000  # 2023-11-14 22:13:20 UTC

# System labels are listed as well, with numeric IDs and typed as folders (3) or labels (1), exactly like this (proton-mail-export lib/tests/test_data/ExportedData.cpp).
SYSTEM_LABELS = [
    {"Path": path, "ID": label_id, "ParentID": "", "Name": name, "Color": "#8080FF", "Type": label_type}
    for label_id, name, path, label_type in [
        ("0", "Inbox", "", 3), ("8", "Drafts", "", 3), ("1", "All Drafts", "All Drafts", 1),
        ("12", "All Scheduled", "All Scheduled", 1), ("7", "Sent", "", 3), ("2", "All Sent", "All Sent", 1),
        ("10", "Starred", "Starred", 1), ("16", "Snoozed", "Snoozed", 1), ("6", "Archive", "", 3),
        ("4", "Spam", "", 3), ("3", "Trash", "", 3), ("5", "All Mail", "All Mail", 1),
        ("15", "All Mail", "All Mail", 1), ("9", "Outbox", "Outbox", 1),
    ]
]

LABELS = SYSTEM_LABELS + [
    {"ID": "fWork==", "ParentID": "", "Name": "Work", "Path": "Work", "Color": "#000", "Type": 3},
    {"ID": "f2024==", "ParentID": "fWork==", "Name": "2024", "Path": "Work/2024", "Color": "#000", "Type": 3},
    {"ID": "fCafe==", "ParentID": "", "Name": "Café ☕", "Path": "Café ☕", "Color": "#000", "Type": 3},
    {"ID": "fDots==", "ParentID": "", "Name": "v1.2", "Path": "v1.2", "Color": "#000", "Type": 3},
    {"ID": "fCur==", "ParentID": "fWork==", "Name": "cur", "Path": "Work/cur", "Color": "#000", "Type": 3},
    {"ID": "lTodo==", "ParentID": "", "Name": "Todo", "Path": "Todo", "Color": "#000", "Type": 1},
    {"ID": "lFollow==", "ParentID": "", "Name": "Follow up", "Path": "Follow up", "Color": "#000", "Type": 1},
    {"ID": "lEmpty==", "ParentID": "", "Name": "Unused", "Path": "Unused", "Color": "#000", "Type": 1},
]

# name: (label IDs, Flags, unread, replied, forwarded)
MESSAGES = {
    "inbox_unread": (["0", "5", "15", "24"], RECEIVED, 1, 0, 0),
    "inbox_starred": (["0", "5", "15", "10", "lTodo=="], RECEIVED, 0, 1, 1),
    "sent": (["2", "7", "5", "15"], SENT, 0, 0, 0),
    "draft": (["1", "8", "5", "15"], 0, 0, 0, 0),
    "archived": (["6", "5", "15", "lFollow==", "lTodo=="], RECEIVED, 0, 0, 0),
    "work_2024": (["f2024==", "5", "15"], RECEIVED, 0, 0, 0),
    "cafe": (["fCafe==", "5", "15"], RECEIVED, 1, 0, 0),
    "dots": (["fDots==", "5", "15"], RECEIVED, 0, 0, 0),
    "cur_folder": (["fCur==", "5", "15"], RECEIVED, 0, 0, 0),
    "spam": (["4", "5"], RECEIVED, 1, 0, 0),
    "trash": (["3", "5"], RECEIVED, 0, 0, 0),
    "snoozed": (["16", "5", "15"], RECEIVED, 1, 0, 0),
    "to_self": (["0", "2", "7", "5", "15"], RECEIVED | SENT, 0, 0, 0),
    "attachment": (["0", "5", "15"], RECEIVED, 0, 0, 0),
    "unfiled": (["5", "15", "99"], RECEIVED, 0, 0, 0),
    "deleted_label": (["0", "5", "15", "lGone=="], RECEIVED, 0, 0, 0),
    "soft_deleted": (["40", "5"], RECEIVED, 0, 0, 0),
    "failed": (["0", "5", "15"], RECEIVED, 0, 0, 0),
}

# The mailbox each message must end up in, as Bridge would show it.
EXPECTED_MAILBOXES = {
    "INBOX": {"inbox_unread", "inbox_starred", "snoozed", "to_self", "attachment", "deleted_label"},
    "Sent": {"sent", "to_self"},
    "Drafts": {"draft"},
    "Archive": {"archived"},
    "Trash": {"trash"},
    "Spam": {"spam"},
    "Folders/Work": set(),
    "Folders/Work/2024": {"work_2024"},
    "Folders/Work/cur": {"cur_folder"},
    "Folders/Café ☕": {"cafe"},
    "Folders/v1.2": {"dots"},
    "Labels/Todo": {"inbox_starred", "archived"},
    "Labels/Follow up": {"archived"},
    "Labels/Unused": set(),
    "Unfiled": {"unfiled"},
}
MAILDIR_NAMES = {"Folders/Work/cur": "Folders/Work/cur_"}  # directory names that differ from the mailbox name

EXPECTED_FLAGS = {
    "inbox_unread": set(),
    "inbox_starred": {"\\Seen", "\\Flagged", "\\Answered", "$Forwarded", "Forwarded"},
    "sent": {"\\Seen"},
    "draft": {"\\Seen", "\\Draft"},
    "spam": set(),
    "cafe": set(),
    "snoozed": set(),
}


def maildir_name(mailbox: str) -> str:
    return MAILDIR_NAMES.get(mailbox, mailbox)


def message_id(name: str) -> str:
    """Proton IDs are URL-safe base64."""
    return f"{name.replace('_', '-')}_Xy-{len(name):02d}AbCdEfGh==="


def message_time(name: str) -> int:
    return T0 + list(MESSAGES).index(name) * 3600


def eml(name: str, received: int) -> bytes:
    headers = [
        "Return-Path: <alice@example.org>",
        "X-Original-To: me@example.com",
        "Delivered-To: me@example.com",
        f"Received: from mail.example.org by mail.proton.me; {received}",
        f"Subject: {name}",
        "From: Alice <alice@example.org>",
        "To: me@example.com",
        f"Message-Id: <{name}@example.org>",
        "Date: Tue, 14 Nov 2023 22:13:20 +0000",
        f"X-Pm-Internal-Id: {message_id(name)}",
        "X-Pm-Date: Tue, 14 Nov 2023 22:13:20 +0000",
        "Mime-Version: 1.0",
    ]
    if name == "attachment":
        headers.append('Content-Type: multipart/mixed; boundary="b1"')
        body = ("--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee attachment.\r\n"
                "--b1\r\nContent-Type: application/pdf; name=\"a.pdf\"\r\n"
                "Content-Disposition: attachment; filename=\"a.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                "JVBERi0xLjQKJcOkw7zDtsOfCg==\r\n--b1--\r\n")
    else:
        headers.append("Content-Type: text/plain; charset=utf-8")
        body = f"Body of {name}.\r\n"
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


def write_versioned(path: Path, payload) -> None:
    path.write_text(json.dumps({"Version": 1, "Payload": payload}, indent=2, ensure_ascii=False))


def make_export(parent: Path, stamp: str, messages: dict, unread_overrides: dict | None = None) -> Path:
    export = parent / f"mail_{stamp}"
    export.mkdir(parents=True)
    write_versioned(export / "labels.json", LABELS)
    for i, (name, (label_ids, flags, unread, replied, forwarded)) in enumerate(messages.items()):
        msg_id, received = message_id(name), T0 + i * 3600
        if unread_overrides and name in unread_overrides:
            unread = unread_overrides[name]
        writer_type = 1 if name == "failed" else 0
        write_versioned(export / f"{msg_id}.metadata.json", {
            "ID": msg_id, "AddressID": "addr==", "LabelIDs": label_ids, "ExternalID": f"{name}@example.org",
            "Subject": name, "Sender": {"Name": "Alice", "Address": "alice@example.org"},
            "ToList": [{"Name": "", "Address": "me@example.com"}], "CCList": None, "BCCList": None,
            "ReplyTos": [{"Name": "Alice", "Address": "alice@example.org"}],
            "Flags": flags, "Time": received, "Size": 100, "Unread": unread, "IsReplied": replied,
            "IsRepliedAll": 0, "IsForwarded": forwarded, "NumAttachments": 0, "Attachments": None,
            "MIMEType": "text/plain", "Headers": "", "WriterType": writer_type,
        })
        if writer_type:
            (export / msg_id).mkdir()
            (export / msg_id / "body.txt").write_text("could not be assembled")
        else:
            (export / f"{msg_id}.eml").write_bytes(eml(name, received))
    return export


def convert(*args: str) -> subprocess.CompletedProcess:
    """Run the proton2maildir command."""
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
