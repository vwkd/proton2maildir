#!/usr/bin/env python3
"""Convert a Proton Mail Export Tool export into a Maildir.

The Export Tool writes one directory per run:

    mail_YYYYMMDD_HHMMSS/
        labels.json            folders and labels
        <ID>.eml               the message, built by the same code Proton Mail Bridge uses
        <ID>.metadata.json     LabelIDs, Unread, IsReplied, IsForwarded, Flags, Time, ...

proton2maildir files, flags and dates the messages the way Bridge shows them over IMAP and writes them as a Maildir tree: one directory per mailbox with cur/, new/ and tmp/, and subfolders as nested directories.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import datetime
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"
__all__ = ["Error", "Export", "Message", "find_export", "read_export", "write_maildir", "main"]

# The result mirrors what Proton Mail Bridge shows over IMAP; references to Bridge's code are to proton-bridge v3.10.0, internal/services/imapservice/. The .eml files already match it: the Export Tool builds them with Bridge's message builder and the same options (proton-mail-export's go-lib/internal/mail/export_stage_build.go and Bridge's sync_build.go both define defaultMessageJobOpts), so they carry Bridge's X-Pm-* headers and a Message-Id on every message.

EXPORT_FORMAT_VERSION = 1  # "Version" field of labels.json and *.metadata.json

# Proton system label IDs (go-proton-api, WebClients MAILBOX_LABEL_IDS).
INBOX, ALL_DRAFTS, ALL_SENT, TRASH, SPAM, ALL_MAIL = "0", "1", "2", "3", "4", "5"
ARCHIVE, SENT, DRAFTS, OUTBOX, STARRED = "6", "7", "8", "9", "10"
SCHEDULED, ALMOST_ALL_MAIL, SNOOZED, SOFT_DELETED = "12", "15", "16", "40"
CATEGORIES = {"20", "21", "22", "24", "25", "26"}  # inbox categories (Social, Promotions, ...)

# Labels that are views onto other locations, not places of their own. Starred becomes \Flagged.
VIRTUAL_LABELS = {ALL_DRAFTS, ALL_SENT, ALL_MAIL, STARRED, ALMOST_ALL_MAIL} | CATEGORIES

# Where a message lives, with Bridge's names. Bridge also shows "All Mail" and "Starred" (WantLabel in helpers.go); those would only duplicate messages and are left out. Bridge doesn't show Snoozed, Scheduled or Outbox at all: snoozed mail returns to the inbox, so it goes there.
LOCATION_MAILBOXES = {
    INBOX: "INBOX",
    DRAFTS: "Drafts",
    SENT: "Sent",
    ARCHIVE: "Archive",
    TRASH: "Trash",
    SPAM: "Spam",
    SNOOZED: "INBOX",
    SCHEDULED: "Scheduled",
    OUTBOX: "Outbox",
}
UNFILED_MAILBOX = "Unfiled"  # safety net for messages without any known location
STANDARD_MAILBOXES = ["INBOX", "Drafts", "Sent", "Archive", "Trash", "Spam"]

LABEL_TYPE_LABEL, LABEL_TYPE_FOLDER = 1, 3
MESSAGE_FLAG_RECEIVED, MESSAGE_FLAG_SENT = 1 << 0, 1 << 1
WRITER_TYPE_BUILT = 0  # other writer types mean the message could not be assembled into an .eml

# Maildir letters of IMAP flags; $Forwarded is Maildir's P ("passed"). Keywords ($Forwarded, Forwarded) are also written the way Dovecot stores them: letters a-z, named in dovecot-keywords.
MAILDIR_FLAGS = {"\\Draft": "D", "\\Flagged": "F", "$Forwarded": "P", "\\Answered": "R", "\\Seen": "S"}
MAILDIR_SUBDIRS = ("cur", "new", "tmp")
SUMMARY = ".proton2maildir.json"  # also marks a directory as replaceable by the next run


class Error(Exception):
    pass


@dataclass(frozen=True)
class Message:
    id: str  # Proton message ID
    eml: Path  # the message (RFC 822) written by the Export Tool
    time: int  # Proton's message time in Unix seconds, which Bridge reports as the IMAP INTERNALDATE (toIMAPMessage in helpers.go)
    flags: frozenset[str]  # IMAP flags and keywords, as Bridge shows them
    mailboxes: tuple[str, ...]  # mailbox names, levels separated by "/", as Bridge shows them


@dataclass
class Export:
    path: Path
    mailboxes: list[str] = field(default_factory=list)  # every mailbox, empty folders and labels included
    messages: list[Message] = field(default_factory=list)
    not_converted: list[Path] = field(default_factory=list)  # parts of messages the Export Tool couldn't assemble
    soft_deleted: int = 0
    unknown_label_ids: dict[str, int] = field(default_factory=dict)  # label ID -> number of messages


def find_export(path: Path) -> Path:
    """Return path if it is an export, else the newest export in it or in its subdirectories."""
    if (path / "labels.json").is_file():
        return path
    exports = [p for p in [*path.glob("mail_*"), *path.glob("*/mail_*")] if (p / "labels.json").is_file()]
    if not exports:
        raise Error(f"no Proton Mail export (labels.json) found in {path}")
    if len({p.parent for p in exports}) > 1:
        raise Error(f"exports of several accounts found in {path}; pass one export directory:\n  "
                    + "\n  ".join(map(str, sorted(exports))))
    return max(exports, key=lambda p: p.name)


def read_versioned_json(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("Version") != EXPORT_FORMAT_VERSION:
        raise Error(f"{path}: unsupported export format version {data.get('Version')!r} "
                    f"(expected {EXPORT_FORMAT_VERSION})")
    return data["Payload"]


def custom_mailboxes(labels: list[dict]) -> dict[str, tuple[str, bool]]:
    """Map custom label IDs to (mailbox name, is folder), named like Bridge's GetMailboxName (imap_updates.go) does, e.g. "Folders/Work/2024" or "Labels/Todo"."""
    result = {}
    for label in labels:
        if label["ID"].isdigit():
            continue  # system labels are listed too, typed as folders or labels (export tool: isSystemLabel)
        prefix = {LABEL_TYPE_FOLDER: "Folders", LABEL_TYPE_LABEL: "Labels"}.get(label["Type"])
        if prefix:
            parts = [p for p in (label.get("Path") or label["Name"]).split("/") if p]
            result[label["ID"]] = ("/".join([prefix, *parts]), label["Type"] == LABEL_TYPE_FOLDER)
    return result


def message_mailboxes(meta: dict, custom: dict[str, tuple[str, bool]], unknown: collections.Counter) -> list[str]:
    locations, labels = [], []
    for label_id in meta["LabelIDs"]:
        if label_id in LOCATION_MAILBOXES:
            locations.append(LOCATION_MAILBOXES[label_id])
        elif label_id in custom:
            name, is_folder = custom[label_id]
            (locations if is_folder else labels).append(name)
        elif label_id not in VIRTUAL_LABELS:
            unknown[label_id] += 1
    if not locations:
        locations.append(UNFILED_MAILBOX)
    return list(dict.fromkeys(locations + labels))


def message_flags(meta: dict) -> frozenset[str]:
    """IMAP flags, mapped like Bridge's BuildFlagSetFromMessageMetadata (helpers.go)."""
    flags = set()
    if not meta["Unread"]:
        flags.add("\\Seen")
    if STARRED in meta["LabelIDs"]:
        flags.add("\\Flagged")
    if not meta["Flags"] & (MESSAGE_FLAG_RECEIVED | MESSAGE_FLAG_SENT):
        flags.add("\\Draft")
    if meta["IsReplied"] or meta["IsRepliedAll"]:
        flags.add("\\Answered")
    if meta["IsForwarded"]:
        flags |= {"$Forwarded", "Forwarded"}
    return frozenset(flags)


def read_export(path: Path) -> Export:
    """Read an export directory (mail_YYYYMMDD_HHMMSS)."""
    custom = custom_mailboxes(read_versioned_json(path / "labels.json") or [])
    mailboxes = dict.fromkeys(STANDARD_MAILBOXES + sorted(name for name, _ in custom.values()))
    export = Export(path)
    unknown: collections.Counter = collections.Counter()
    for meta_path in sorted(path.glob("*.metadata.json")):
        meta = read_versioned_json(meta_path)
        meta["LabelIDs"] = meta.get("LabelIDs") or []  # Go writes null for an empty list
        eml = path / f"{meta['ID']}.eml"
        if SOFT_DELETED in meta["LabelIDs"]:
            export.soft_deleted += 1
        elif meta.get("WriterType", WRITER_TYPE_BUILT) != WRITER_TYPE_BUILT or not eml.is_file():
            export.not_converted.append(path / meta["ID"])
        else:
            names = message_mailboxes(meta, custom, unknown)
            mailboxes.update(dict.fromkeys(names))
            export.messages.append(Message(meta["ID"], eml, meta["Time"], message_flags(meta), tuple(names)))
    export.mailboxes = list(mailboxes)
    export.unknown_label_ids = dict(unknown)
    return export


def dir_name(part: str) -> str:
    """Directory name for one level of a mailbox name."""
    part = "".join("_" if ord(c) < 32 or ord(c) == 127 else c for c in part)
    if part in (".", ".."):
        return part.replace(".", "_")
    if part.lower() in MAILDIR_SUBDIRS:  # would be taken for the parent's own cur/new/tmp
        return part + "_"
    return part


def mailbox_dir(maildir: Path, name: str) -> Path:
    return maildir.joinpath(*map(dir_name, name.split("/")))


def load_clonefile():
    if sys.platform != "darwin":
        return None
    clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    return clonefile


CLONEFILE = load_clonefile()


def clone_or_copy(src: Path, dst: Path) -> None:
    """APFS clone where possible: no extra disk space, and unlike a hard link the export's own file keeps its mtime when we set the copy's. Falls back to copying (other volume, not APFS)."""
    if CLONEFILE is None or CLONEFILE(os.fsencode(src), os.fsencode(dst), 0) != 0:
        shutil.copyfile(src, dst)


def write_maildir(export: Export, maildir: Path) -> dict:
    """Write export as a Maildir tree, replacing an earlier proton2maildir output at maildir.

    Returns the summary that is also written to maildir/.proton2maildir.json."""
    if maildir.exists() and any(maildir.iterdir()) and not (maildir / SUMMARY).is_file():
        raise Error(f"{maildir} is not empty and was not written by proton2maildir")
    building = maildir.with_name(f".{maildir.name}.proton2maildir-new")
    shutil.rmtree(building, ignore_errors=True)

    by_mailbox: dict[str, list[Message]] = {name: [] for name in export.mailboxes}
    for message in export.messages:
        for name in message.mailboxes:
            by_mailbox[name].append(message)
    for name, messages in by_mailbox.items():
        path = mailbox_dir(building, name)
        for sub in MAILDIR_SUBDIRS:
            (path / sub).mkdir(parents=True, exist_ok=True)
        keywords = sorted({flag for message in messages for flag in message.flags if not flag.startswith("\\")})
        letters = {keyword: chr(ord("a") + i) for i, keyword in enumerate(keywords)}
        if keywords:
            (path / "dovecot-keywords").write_text("".join(f"{i} {k}\n" for i, k in enumerate(keywords)))
        for message in messages:
            info = "".join(sorted(MAILDIR_FLAGS[f] for f in message.flags if f in MAILDIR_FLAGS))
            info += "".join(sorted(letters[f] for f in message.flags if f in letters))
            target = path / "cur" / f"{message.time}.{message.id}.proton:2,{info}"
            clone_or_copy(message.eml, target)
            if message.time > 0:
                os.utime(target, (message.time, message.time))  # delivery time, Dovecot's INTERNALDATE

    summary = {
        "version": __version__,
        "export": str(export.path),
        "converted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "messages_in_export": len(export.messages) + len(export.not_converted) + export.soft_deleted,
        "mailboxes": {name: len(messages) for name, messages in by_mailbox.items()},
        "skipped_soft_deleted": export.soft_deleted,
        "not_converted": [str(p) for p in export.not_converted],
        "unknown_label_ids": export.unknown_label_ids,
    }
    (building / SUMMARY).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    if maildir.exists():
        previous = maildir.with_name(f".{maildir.name}.proton2maildir-old")
        shutil.rmtree(previous, ignore_errors=True)
        maildir.rename(previous)
        building.rename(maildir)
        shutil.rmtree(previous)
    else:
        building.rename(maildir)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="proton2maildir", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export", type=Path,
                        help="export directory (mail_YYYYMMDD_HHMMSS), or a directory containing exports to use the newest")
    parser.add_argument("maildir", type=Path,
                        help="output directory; replaced if it holds an earlier proton2maildir output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        path = find_export(args.export.expanduser().resolve())
        print(f"Converting {path}")
        maildir = args.maildir.expanduser().resolve()
        summary = write_maildir(read_export(path), maildir)
    except Error as error:
        sys.exit(f"proton2maildir: error: {error}")

    counts = summary["mailboxes"]
    width = max(map(len, counts))
    for name, count in counts.items():
        print(f"  {name:<{width}}  {count:>7}")
    print(f"{summary['messages_in_export']} messages in export, {sum(counts.values())} Maildir entries in {maildir}")
    if summary["skipped_soft_deleted"]:
        print(f"Skipped {summary['skipped_soft_deleted']} soft-deleted messages.")
    if summary["unknown_label_ids"]:
        print("Ignored unknown label IDs (message count): "
              + ", ".join(f"{k} ({v})" for k, v in sorted(summary["unknown_label_ids"].items())))
    if summary["not_converted"]:
        print(f"WARNING: {len(summary['not_converted'])} messages could not be decrypted or assembled into an .eml "
              "by the Export Tool; their parts are in these directories:", file=sys.stderr)
        for path in summary["not_converted"]:
            print(f"  {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
