# apple-suite-mcp

One small Python file that gives Claude (or any MCP client) access to the
built-in Apple apps on your Mac: **Mail, Contacts, Calendar, Notes, and
Reminders.**

Because it talks to the Mail *app* rather than any one email service, it sees
every account you've added to Mail — iCloud, Gmail, Google Workspace,
Exchange — with no OAuth setup and no extra logins.

## Why this one

There are several Apple MCPs out there. This one optimizes for three things:

- **Minimal.** One readable `server.py`, three dependencies, no Node, no app
  to install. If you want to know what it does, you can just read it.
- **Fast where it matters.** Calendar and Reminders use EventKit (Apple's
  native database API) instead of scripting the Calendar app — hundreds of
  times faster on real calendars. Mail listing and search read Mail's local
  SQLite index directly when Full Disk Access is granted (milliseconds even
  on giant mailboxes), and fall back to AppleScript automatically when it
  isn't.
- **Self-diagnosing.** macOS permissions are the hard part of every Apple
  MCP. `python server.py check` (or the `check_access` tool) tells you
  exactly what's granted, what's missing, and which System Settings pane
  fixes it.

## Setup

Requires macOS and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```sh
git clone https://github.com/travisthetrashpanda/apple-suite-mcp.git ~/apple-suite-mcp
```

**Claude Code:**

```sh
claude mcp add --scope user apple -- uv run --directory ~/apple-suite-mcp python server.py
```

**Claude Desktop / any MCP client** — add to your MCP config (e.g.
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "apple": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/YOU/apple-suite-mcp", "python", "server.py"]
    }
  }
}
```

Use the full path to `uv` (usually `/opt/homebrew/bin/uv`) if your client
doesn't inherit your shell's PATH.

### Permissions (one-time)

Run the diagnosis and follow what it tells you:

```sh
cd ~/apple-suite-mcp && uv run python server.py check
```

- **Automation** (Mail, Contacts, Notes): macOS prompts on first use — click
  Allow.
- **Calendars & Reminders**: set the host app (e.g. Claude) to **Full
  Access** in System Settings → Privacy & Security.
- **Full Disk Access** (optional): enables the fast mail path. Without it,
  everything still works — large mailboxes are just slow (~30s per query).
  This is a broad grant; read the code and decide for yourself.

## Try it

> what's unread across my inboxes?
>
> search all my accounts for emails from the DMV
>
> what's on my calendar this week?
>
> remind me Thursday at 9am to renew the registration
>
> find my note about the wifi setup

## Scoping access (optional)

macOS permissions are per-*app*, not per-account — once the host app can
control Mail, it can see every account on the Mac. If you want tighter
control (say, a shared work/personal machine), copy `config.example.json`
to `config.json` and list what the server is allowed to see:

```json
{
  "mail_accounts": ["Personal"],
  "calendars": ["Home", "Family"],
  "reminder_lists": ["Reminders"],
  "note_folders": ["Notes"],
  "disabled_tools": ["mail_send"]
}
```

Anything not listed is invisible to every tool — it can't be listed, read,
searched, or modified. Omit a key (or the whole file) to allow everything
of that kind. `disabled_tools` removes tools entirely (they're never even
registered with the MCP client). `check_access` reports the active scoping.

## Tools

| App | Tools |
|---|---|
| Mail | `mail_accounts`, `mail_unread_counts`, `mail_messages`, `mail_search`, `mail_read_message`, `mail_send`, `mail_mark_read`, `mail_move`, `mail_trash` |
| Contacts | `contacts_search` |
| Calendar | `calendar_list`, `calendar_events`, `calendar_create_event` |
| Notes | `notes_folders`, `notes_search`, `notes_read`, `notes_create` |
| Reminders | `reminders_list`, `reminders_create`, `reminders_complete` |
| — | `check_access` |

Test any tool from the command line without an MCP client:

```sh
uv run python server.py test mail_unread_counts
uv run python server.py test mail_messages '{"account": "Work", "limit": 5}'
```

## Safety notes

- `mail_trash` uses Mail's normal reversible delete (messages go to the
  account's Trash). There is deliberately **no permanent-delete tool**.
- `mail_send` sends real email through your accounts; MCP clients should
  confirm with the user before calling it.
- Everything runs locally — nothing is sent anywhere except by `mail_send`.

## Limitations

- macOS only; the target apps must be configured (Mail needs at least one
  account).
- The first Mail query right after Mail launches can time out while it
  syncs — retry after a minute.
- Without Full Disk Access, very large mailboxes take ~30s per query and
  cross-account search (`mail_search` with no account) is unavailable.
- Nested mail folders are addressed by their full path (e.g.
  `"[Gmail]/All Mail"`).

## Credits

- [sweetrb/apple-mail-mcp](https://github.com/sweetrb/apple-mail-mcp) — the
  project that proved the scripted-Mail approach and inspired this one.
- [krmj22/macos-mcp](https://github.com/krmj22/macos-mcp) — the idea of
  reading Mail's Envelope Index directly instead of fighting slow
  AppleScript reads.

## Say thanks

This project is free to use, no strings attached. If it's useful to you, a ⭐
on this repo or a shout-out to
[@travisthetrashpanda](https://github.com/travisthetrashpanda) is always
appreciated — and if you build something on top of it, a link back here
helps others find it.

## License

MIT
