# proton2maildir

Convert a [Proton Mail Export Tool](https://proton.me/support/proton-mail-export-tool) export into a Maildir.

The Export Tool, free on every Proton plan, saves each message as an `.eml` file next to a JSON file with its folder, labels and flags. proton2maildir turns that into a Maildir tree: messages are filed, flagged and dated the way [Proton Mail Bridge](https://proton.me/mail/bridge) shows them over IMAP. From there, other tools take over, for example Dovecot to serve the mail over IMAP and imapsync to copy it to another provider ([example](examples/imapsync)).

Python 3.9 or newer, standard library only. The export is only read.

## Why

Proton Mail has no IMAP access of its own: Proton Mail Bridge provides it, but only on paid plans. The Export Tool is free, but mail tools don't understand its output. They can open the `.eml` files, but the folders, labels and read/starred state are in Proton-specific JSON. proton2maildir converts both into a standard Maildir, so that existing, well-tested tools can do the rest.

Alternatives:

- **One month of a paid plan, and Bridge**: imapsync can then copy straight from Bridge's local IMAP server (127.0.0.1:1143), excluding its All Mail and Starred views, and Labels if you don't want the label copies. That's simpler if paying is fine, but every later run to copy new mail needs the paid plan again.
- **[hydroxide](https://github.com/emersion/hydroxide)**: a third-party bridge that works with free accounts, but its IMAP support is marked as work in progress.
- **Thunderbird with ImportExportTools NG**: imports the `.eml` files but not the JSON, so folders, labels and read/starred state are lost.

## Install

From a checkout:

```bash
pip install .
```

Or `uv tool install .`, or run [src/proton2maildir.py](src/proton2maildir.py) directly; it's a single file.

## Usage

Export your mailbox with the Export Tool. On macOS, download [proton-mail-export-cli-macos.zip](https://proton.me/download/export-tool/proton-mail-export-cli-macos.zip) (there's no Homebrew cask; [Proton's page](https://proton.me/support/proton-mail-export-tool) also has Windows and Linux builds), unzip it, open `proton-mail-export-cli.app`, sign in and choose "Backup". It saves to `~/Downloads/proton-mail-export-cli/<address>/mail_YYYYMMDD_HHMMSS/`.

To export from a terminal instead, run the following. It still asks for the password and 2FA code, writes to `~/proton-export/mail_YYYYMMDD_HHMMSS/`, and `--telemetry` turns telemetry off:

```bash
proton-mail-export-cli.app/Contents/MacOS/proton-mail-export-cli --operation backup --dir ~/proton-export --user you@proton.me --telemetry
```

Each run exports the whole mailbox, and an interrupted export can't be resumed. Then convert the export:

```bash
proton2maildir ~/Downloads/proton-mail-export-cli ~/proton-maildir
```

The first argument is an export directory, or a directory containing exports, in which case the newest is used. The second is the Maildir to write. A non-empty directory there is only replaced if it holds an earlier proton2maildir output.

The command prints how many messages went into each mailbox. It also lists messages the Export Tool couldn't decrypt or assemble into an `.eml`; those stay in the export as separate parts and aren't converted.

The export and the Maildir hold your mail unencrypted. Delete them when you no longer need them.

From Python:

```python
from pathlib import Path
from proton2maildir import read_export, write_maildir

export = read_export(Path("mail_20261005_233638"))
for message in export.messages:
    print(message.mailboxes, sorted(message.flags), message.time, message.eml)
write_maildir(export, Path("proton-maildir"))
```

`read_export` only reads, so you can also use it to feed the messages into something other than a Maildir.

## Output

```
proton-maildir/
├── .proton2maildir.json      summary of the conversion
├── INBOX/                    cur/, new/, tmp/
├── Drafts/  Sent/  Archive/  Trash/  Spam/
├── Folders/Work/             a folder, with its subfolder in Folders/Work/2024/
└── Labels/Todo/              a label, with a copy of every message that has it
```

Each mailbox is a directory with `cur/`, `new/` and `tmp/`, and subfolders are nested inside their parent's directory. Dovecot calls this layout `fs` and mbsync calls it `Verbatim`. Names are UTF-8. A name level that would be mistaken for Maildir internals gets a `_` appended (`cur`, `new` and `tmp`, in any case), `.` and `..` become `_` and `__`, and control characters become `_`.

### Mailboxes

| Proton | Mailbox |
| --- | --- |
| Inbox, and snoozed messages | `INBOX` |
| Drafts, Sent, Archive, Trash, Spam | `Drafts`, `Sent`, `Archive`, `Trash`, `Spam` |
| folder `A/B` | `Folders/A/B` |
| label `X` | `Labels/X` |
| Scheduled, Outbox | `Scheduled`, `Outbox` |
| none of the above | `Unfiled` (shouldn't happen) |
| All Mail, All Sent, All Drafts, Starred, inbox categories | no mailbox: they would only duplicate messages |

The names follow Bridge. Bridge also has views like All Mail and Starred, which would only duplicate messages, and doesn't show snoozed, scheduled or outbox messages at all. Empty folders and labels get a mailbox too.

### Flags and dates

| Proton | IMAP flag (as in Bridge) | Maildir |
| --- | --- | --- |
| read | `\Seen` | `S` |
| starred | `\Flagged` | `F` |
| replied, or replied to all | `\Answered` | `R` |
| forwarded | `$Forwarded`, `Forwarded` | `P`, and the keywords |
| draft (neither sent nor received) | `\Draft` | `D` |

Keywords are stored the way Dovecot stores them: as lowercase letters in the file name, named in the mailbox's `dovecot-keywords` file.

Each file name starts with Proton's message time, and the file's modification time is set to it. Dovecot reports the modification time as the IMAP INTERNALDATE: the received date that mail clients sort by.

The message files are the Export Tool's `.eml` files, unchanged. On APFS (macOS) they are clones, which take no extra disk space; elsewhere they are copies.

Not converted:

- messages the Export Tool couldn't assemble (listed by the command),
- soft-deleted messages,
- metadata that only Proton has: label colors, inbox categories, snooze and scheduled-send times, expiration.

### Running it again

The Export Tool always exports the whole mailbox, and proton2maildir rebuilds the Maildir from one export. Tools that recognize messages by their headers, like imapsync, can therefore copy only the new messages from a later export. Tools that keep their own state about the files in a Maildir would see a rebuilt tree as new messages.

## Examples

- [Copy to an IMAP server with imapsync](examples/imapsync), with MXroute as an example

## Development

```bash
python3 -m unittest -v
```

The tests use a synthetic export in the Export Tool's format, modeled on the tool's source and test data. If `dovecot`, `socat` and `imapsync` are installed, they also run the [imapsync example](examples/imapsync) with its config and commands as written. The copy goes into a local Dovecot set up like MXroute and is checked message by message. To run that check on a real export:

```bash
PROTON_EXPORT=~/Downloads/proton-mail-export-cli python3 -m unittest tests.test_imapsync_example.RealExportTest -v
```
