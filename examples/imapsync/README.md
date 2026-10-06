# Copy to an IMAP server with imapsync

[imapsync](https://imapsync.lamiral.info/) copies mail between IMAP servers, keeping flags and received dates. It skips messages the destination already has, so you can run it again later to copy only new mail. Its source has to be an IMAP server: [Dovecot](https://www.dovecot.org/) can serve the Maildir with [dovecot.conf](dovecot.conf), using its `imap` binary through [socat](http://www.dest-unreach.org/socat/) instead of running a Dovecot daemon.

## Requirements

```bash
brew install dovecot imapsync socat
```

Dovecot has to be 2.4 or newer.

## Usage

Run the commands from the repository root. Convert the export, then serve the Maildir on 127.0.0.1:1143 and leave it running (Ctrl-C stops it):

```bash
proton2maildir ~/Downloads/proton-mail-export-cli ~/proton-maildir
```

```bash
PROTON_MAILDIR=~/proton-maildir socat TCP-LISTEN:1143,bind=127.0.0.1,reuseaddr,fork EXEC:"doveadm -c examples/imapsync/dovecot.conf exec imap"
```

Each connection gets a Dovecot `imap` process that is already logged in (PREAUTH), without a password. While it runs, anyone who can connect to 127.0.0.1 on this machine can read the mail.

In a second terminal, copy to the destination:

```bash
imapsync \
  --host1 127.0.0.1 --port1 1143 --nossl1 --notls1 --user1 proton --authmech1 PREAUTH \
  --host2 imap.example.com --ssl2 --sslargs2 SSL_verify_mode=1 --user2 you@example.com --passfile2 ~/.imap-password \
  --automap --subscribeall
```

- `--sslargs2 SSL_verify_mode=1` makes imapsync check the server's certificate and host name, which it doesn't do by default.
- `--automap` copies Drafts, Sent, Archive, Trash and Spam into the destination's folders for drafts, sent mail and so on, whatever their names are there. `--subscribeall` subscribes every copied folder, because many mail clients only show subscribed folders.
- Keep the password in a file only you can read (`chmod 600`). With `--password2` it would show up in `ps` and in imapsync's log.
- Add `--dry --justfolders` to see the folder mapping without copying anything. Against an empty account, dry runs report "Mailbox doesn't exist" for folders they would create; that's expected.
- imapsync writes a log to `./LOG_imapsync/` and keeps a pidfile in `~/tmp`. Unless you add `--noreleasecheck`, it also checks online for new releases. You can stop it at any time and start it again. On a laptop, prefix the command with `caffeinate -i` to keep the Mac awake.

### Choosing what to copy

All of these are standard imapsync options. Folder names are the Maildir's, with `/` between levels.

| Copy only… | imapsync options |
| --- | --- |
| some folders | `--folder INBOX --folder 'Folders/Work'` |
| a folder and its subfolders | `--include '^Folders/Work(/\|$)'` |
| everything except the label copies | `--exclude '^Labels(/\|$)'` |
| mail received since a date | `--search1 'SINCE 1-Jan-2024'` |
| mail to or from one of your addresses | `--search1 'OR HEADER Delivered-To me@example.com FROM me@example.com'` |

### Copying new mail later

Export again, convert into the same directory (proton2maildir replaces its earlier output), start socat again, and run the same imapsync command with `--noresyncflags` added. imapsync recognizes messages that are already on the destination by their `Message-Id` and `Received` headers (every exported message has a `Message-Id`), so it only copies the new ones. `--noresyncflags` leaves the flags of messages copied before as they are on the destination; without it, imapsync sets them to Proton's current read and star state.

A message you moved to another folder in Proton since the last run gets copied into its new folder as well, and the old copy stays. Messages deleted in Proton stay on the destination. Don't use imapsync's `--delete2`: it deletes everything on the destination that isn't in the Maildir, including mail that arrived there directly.

## MXroute

[MXroute](https://mxroute.com) runs Dovecot with an empty namespace prefix and `.` as the separator between folder levels. Sent, Drafts, Trash, Junk and Archive are top-level folders, and folders you create go under INBOX ([IMAP Path Prefix](https://docs.mxroute.com/docs/general/imap-path-prefix.html), [default folders](https://docs.mxroute.com/docs/default-folders.html)). Add options to match that layout:

```bash
imapsync \
  --host1 127.0.0.1 --port1 1143 --nossl1 --notls1 --user1 proton --authmech1 PREAUTH \
  --host2 yourserver.mxrouting.net --ssl2 --sslargs2 SSL_verify_mode=1 --user2 you@yourdomain.com --passfile2 ~/.mxroute-password \
  --automap --subscribeall --f1f2 Spam=Junk \
  --regextrans2 's{^Folders\.}{INBOX.}' \
  --regextrans2 's{^(Labels|Scheduled|Outbox|Unfiled)(?=\.|$)}{INBOX.$1}' \
  --noresyncflags
```

| Maildir | MXroute |
| --- | --- |
| `INBOX`, `Drafts`, `Sent`, `Archive`, `Trash` | the same |
| `Spam` | `Junk` |
| `Folders/A/B` | `INBOX.A.B` |
| `Labels/X` | `INBOX.Labels.X` |
| `Scheduled`, `Outbox`, `Unfiled` | `INBOX.Scheduled`, `INBOX.Outbox`, `INBOX.Unfiled` |

- **Server**: MXroute's [setup guide](https://docs.mxroute.com/docs/quick-setup.html) says the IMAP host is the same as your primary MX record (Control Panel → DNS), e.g. `yourserver.mxrouting.net`. Use the full email address as username. Use `mail.yourdomain.com` only if you've set it up as a [custom hostname](https://docs.mxroute.com/docs/branding/customhostnames.html) with a certificate.
- **Dots in names**: because `.` separates folder levels, imapsync writes a dot inside a folder name as `_` (`v1.2` becomes `v1_2`).
- **Spam**: MXroute would also treat a folder named `Spam` as junk, but you'd end up with two junk folders.
- **Storage**: copies count against your plan's quota, including the label copies. To avoid those, add `--exclude '^Labels(/|$)'`.
- **Login**: MXroute [blocks IP addresses for 24 hours](https://docs.mxroute.com/docs/troubleshooting/smtp-blocks.html) after repeated authentication failures. That page is about SMTP, but check the password with `--justlogin` before letting imapsync retry a wrong one.
- **Flags**: MXroute's newer servers run Dovecot 2.4, whose default settings add `$HasAttachment`/`$HasNoAttachment` keywords to uploaded mail. Resyncing flags (imapsync's default) would remove those again on every later run; that's another reason for `--noresyncflags`.
- **Limits**: the 400 emails per hour limit is for sending. Uploading over IMAP isn't sending.
