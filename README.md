<div align="center">

# Thread Promoter

**Convert a Discord thread into a dedicated channel, history included.**

A thread cannot be turned into a channel by the API, so this bot creates the
channel and replays the conversation into it through a webhook, preserving each
author's username and avatar. One slash command, run from the thread itself.

[![tests](https://github.com/Pasouvenla/thread-promoter/actions/workflows/tests.yml/badge.svg)](https://github.com/Pasouvenla/thread-promoter/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.7-5865F2?logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)

</div>

---

## Features

- **Faithful replay**: usernames, avatars, attachments, stickers, embeds, polls and pins, re-emitted in order through a webhook
- **Read-only source**: nothing is ever written to the original thread. That is what makes an unsatisfying run repeatable, since you can delete the target channel and start over
- **Preview before committing**: `/promote-preview` shows what a replay would produce, and creates nothing at all
- **Resumable**: an interrupted run picks up where it stopped, reading the target channel rather than trusting a stored cursor
- **Stoppable**: `/promote-abort` ends a run cleanly at a message boundary, leaving a state that can be resumed
- **Verifiable**: `/promote-verify` counts both sides afterwards instead of trusting the run's own report
- **Nothing lost in silence**: anything that cannot cross over is stated under the message concerned and recorded in a manifest
- **Repairable failures**: a failed message holds its slot with a placeholder, and a single choice at the end repairs the whole set in place
- **Rate limit aware**: paced emission, and it counts the throttling discord.py absorbs so you can tune the pacing on evidence
- **Self-hosted**: one container, read-only root, no database, no telemetry

## Limitations

Read this before spending an evening on the setup. A replayed channel is a copy,
not the original. None of the following is a bug, and none of it is fixable:
these are Discord's limits, not this bot's.

- **Replayed messages carry the APP badge.** No API impersonates a user account, and that is a good thing. Usernames and avatars are the original authors', the badge stays
- **Reactions are not replayed by default.** A bot can only react as itself, so replaying them attributes everyone's reactions to the bot. Counts and names are kept as text under each message instead. `PROMOTER_REPLAY_REACTIONS=true` restores the pills, knowing they are all the bot's
- **Attachments above the server's upload limit do not cross.** 10 MB without boosts, 50 MB at level 2, 100 MB at level 3. Anything larger is named and sized under its message, flagged in the manifest, and kept as a local copy so you can republish it elsewhere
- **Timestamps are not real at the protocol level.** A snowflake encodes its own creation time and cannot be forged, so replayed messages are dated from the replay. The original date is rendered as subtext under each message
- **Native replies do not survive as such.** A webhook cannot emit one, and a reply never crosses a channel boundary anyway. Replies become a subtext link to the already-replayed copy
- **Interactive components from other bots are lost.** Buttons and select menus do not survive
- **A large thread takes hours.** Roughly 35 minutes per thousand messages at the default pacing

If any of that is a dealbreaker, `/promote-link` creates the channel and leaves
the history where it is, which loses nothing because it copies nothing.

## Quick start

### Discord application

Create the application at [discord.com/developers](https://discord.com/developers/applications),
then in the **Bot** tab enable both privileged intents: **Message Content** and
**Server Members**. Without the former, message content comes back empty and the
replay only carries attachments.

Invite it with the nine permissions it checks for, replacing the client id:

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=536996944&scope=bot+applications.commands
```

`applications.commands` is not optional: without it the bot joins the server but
cannot register its slash commands.

### Docker Compose

```bash
git clone https://github.com/Pasouvenla/thread-promoter.git
cd thread-promoter
cp .env.example .env      # fill in DISCORD_TOKEN and DISCORD_GUILD_IDS
docker compose up -d --build
```

Setting `DISCORD_GUILD_IDS` syncs the commands immediately on the listed guilds.
Left empty, the sync is global and takes up to an hour to propagate.

### First run

Start with `/promote-preview` in the thread you want to promote. It reads the
thread, tells you how many messages would be replayed and how long it would
take, and creates nothing. Then `/promote`, then `/promote-verify`.

## Commands

All commands run **from inside the source thread**, and all require the Manage
Channels permission on the caller.

| Command | What it does |
|---|---|
| `/promote-preview` | Shows what a replay would produce. Creates nothing |
| `/promote [name]` | Creates the channel and replays the full history |
| `/promote-link [name]` | Creates the channel, links back to the thread, replays nothing |
| `/promote-abort` | Stops a running replay at the next message boundary |
| `/promote-resume` | Continues an interrupted migration |
| `/promote-verify` | Counts both sides and reports what is missing |
| `/promote-recover` | Re-opens the recovery choice for messages that failed |
| `/promote-export` | Renders the manifest as an HTML page |
| `/promote-forget` | Clears the checkpoint, touching no channel |

`/promote-link` is the cheap path and worth reaching for first: three API calls,
no rate limiting, no fidelity loss, because nothing is copied. Reach for
`/promote` when the content genuinely has to live in a real channel, for
instance to attach an integration that does not work on threads.

## Configuration

Everything is environment variables. The switch surface is deliberately narrow:
presentation details are fixed in code, because every extra combination is a
code path nobody exercises.

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token |
| `DISCORD_GUILD_IDS` | empty | Guilds to sync commands to immediately |
| `PROMOTER_MESSAGE_DELAY` | `2.0` | Seconds between messages |
| `PROMOTER_REACTION_DELAY` | `0.30` | Seconds between reaction adds |
| `PROMOTER_REPLAY_REACTIONS` | `false` | Re-add reactions, all as the bot |
| `PROMOTER_REPLAY_ATTACHMENTS` | `true` | Download and re-upload attachments |
| `PROMOTER_KEEP_ATTACHMENT_COPIES` | `true` | Keep a local copy of each attachment |
| `PROMOTER_PROGRESS_EVERY` | `25` | Messages between progress updates |
| `PROMOTER_DATA` | named volume | Host path to bind mount instead |

The observed ceiling is around 30 webhook sends per minute per channel, and that
bucket is shared with reactions and pins. The default 2 second pacing leaves
headroom. Those numbers come from documentation rather than measurement, so the
bot counts the rate limits discord.py absorbs on its behalf and reports them in
the progress header and the manifest. Tune the pacing on that, not on this
paragraph.

## How resuming works

Every replayed message carries a jump link naming the message it came from. A
resume reads the target channel back, finds the most recent of those links, and
continues after it. **The channel is its own record of what was done.**

The checkpoint on disk is an optimisation, not the authority. Deleting it, or
finding it corrupted, costs one extra read and nothing else. This is deliberate:
a cursor that drifts out of step with reality is how a tool like this replays a
thousand messages twice.

For the same reason the target channel is resolved through the API rather than a
local cache, and only an explicit 404 is accepted as proof that it is gone. A
channel that merely cannot be reached stops the run with an error instead of
starting over.

## Failed messages

A message that fails is retried three times with growing backoff, which absorbs
the bulk of transient errors. If it still fails, a placeholder is posted in its
place carrying the author, the avatar, the real timestamp and a link to the
original, and the replay carries on rather than stopping.

Because the placeholder holds the slot, the order of the conversation stays
intact and the failure can be repaired **in place** later, by editing that very
message rather than appending a correction at the end.

At the end of the run every failure is listed in one report, and one choice is
applied to the whole set:

- **Force**: every failed message ends up present, degraded as far as necessary: without attachments, then without embeds, then with truncated content, and as a last resort the placeholder stands as a stub. Completeness first
- **Best effort**: tries the full message, then without attachments, then without embeds, and stops there. Anything still failing keeps its placeholder rather than being silently truncated. Fidelity first
- **Leave as is**: keeps every placeholder. The gaps stay visible, the detail stays in the manifest, and `/promote-recover` re-opens the choice later

It is deliberately a single decision for the whole set. A large thread runs
unattended for the better part of an hour, and asking per message would turn a
background job into a babysitting exercise.

## Persisted data

The `/data` volume holds four directories: `state` for the checkpoints, one JSON
file per thread; `manifests` for the full archive of each migration, including
everything that could not be replayed; `attachments` for local copies; and
`exports` for the HTML pages.

**These files are conversation content in the clear**, plus a live webhook token
for as long as a run is in progress. Directories are created `0700` and files
`0600`, and the replay webhook is deleted once a run completes cleanly, which is
the point at which the stored token stops being useful to anyone.

Keep the volume out of routine backup jobs rather than adding it to them, and
purge it once a migration has been validated. Nothing in there is worth
restoring: the source thread is untouched and the run can simply be done again.

## Security notes

The bot needs the **Message Content** intent, which means it can read every
message in every channel it can see. Consider restricting its channel access
while you evaluate it, and removing it from the server once you are done.

The compose file reads secrets through `${...}` substitution rather than an
`env_file:` directive. Substitution honours a file passed with `--env-file`,
which is what lets a deployment tool hand over a decrypted `.env`; `env_file:`
ignores it and injects the file as it stands, encrypted or not.

The container runs read-only as a fixed uid 1000, with `cap_drop: ALL`,
`no-new-privileges`, and a healthcheck watching a heartbeat the bot refreshes
while the gateway is up. A bot that stays connected and stops making progress is
the failure worth catching, and nothing else about a gateway bot is observable
from outside.

## FAQ

**Why not just convert the thread?**
The Discord API cannot change a channel type. A thread carries an immutable
`parent_id` and type, and no endpoint turns it into a `GUILD_TEXT` channel.
Every solution comes down to creating a channel and re-emitting the messages.

**What happens to the original thread?**
Nothing. It is never written to, never locked, never archived, never deleted.
What happens to it afterwards is a server administration decision. As long as it
exists, the jump links keep working and a second pass remains possible.

**Can I run it again if the result is unsatisfying?**
Yes, and that is the point. Delete the target channel, run `/promote-forget` if
you want to clear the state explicitly, and start again against an untouched
source. The next run notices the channel is gone and resets on its own.

**Does it work on forum posts?**
Yes. A forum post's opening message lives inside the thread rather than in the
parent channel, and the replay handles both cases.

**Why did my run stop with a permissions error?**
Discord sends slash commands to a bot regardless of its channel permissions, so
a command can arrive from a channel the bot cannot actually read. If the parent
channel denies `View Channel` to the bot, every other permission is void there,
which is why the error lists all nine at once.

**How many channels can a server hold?**
Five hundred, and fifty per category. Promoting a thread eats into that budget,
which is exactly why `/promote-link` exists.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest ruff
python -m pytest
ruff check app tests
```

The suite runs against a hand-written double of the discord.py surface, needs no
network and no token, and includes regression tests pinning the two properties
that matter: that nothing is ever written to the source thread, and that a
target channel missing from the cache is never mistaken for a deleted one.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
