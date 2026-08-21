# Thread Promoter

A Discord bot that creates a channel named after a thread and replays the whole
thread history into it, as faithfully as the API allows. One slash command, run
from the thread itself.

That is the entire scope. The source thread is read-only: nothing is written to
it, nothing is locked, archived or deleted. What happens to the original
afterwards is the server administrator's call.

## Limitations, before you invest any time in this

A replayed channel is a copy, not the original, and some of what a thread holds
cannot cross over. Read this first: none of it is a bug, and none of it is going
to be fixed, because the limits are Discord's rather than this bot's.

**Replayed messages are posted by a webhook and carry the APP badge.** No API
impersonates a user account, and that is a good thing. Usernames and avatars are
those of the original authors, but the badge stays.

**Reactions are not replayed by default.** A bot can only react as itself, so
replaying reactions attributes everyone's to the bot: pills that look right and
lie about who pressed them. Who reacted with what is kept as text under each
message and in full in the manifest. `PROMOTER_REPLAY_REACTIONS=true` turns the
pills back on if you would rather have them, knowing they are all the bot's.

**Attachments above the target server's upload limit do not cross.** That limit
is 10 MB without boosts, 50 MB at level 2, 100 MB at level 3. Anything larger is
named and sized in subtext under its message and flagged in the manifest, and a
local copy is kept so you can republish it elsewhere. Nothing can be done about
this from a bot: the upload is refused by Discord itself.

**Timestamps are not real at the protocol level.** A Discord snowflake encodes
its own creation time and cannot be forged, so a replayed message is dated from
the moment of the replay. The original date is rendered as subtext under each
message, which is the closest thing available.

**Native replies do not survive as such.** A webhook cannot emit one, and a
reply never crosses a channel boundary anyway. Replies are rebuilt as a subtext
link to the already-replayed copy.

**Interactive components from other bots are lost.** Buttons and select menus do
not survive a replay.

**A large thread takes hours.** Roughly thirty-five minutes per thousand
messages at the default pacing, because of Discord's rate limits.

If any of that is a dealbreaker for what you are trying to do, `/promote-link`
creates the channel and leaves the history where it is, which loses nothing
because it copies nothing.

## Why a replay and not a conversion

The Discord API cannot change a channel type. A thread (types 10, 11 and 12)
carries an immutable `parent_id` and type, and no endpoint turns it into a
`GUILD_TEXT` channel. Every solution therefore comes down to creating a new
channel and re-emitting the messages, here through a webhook so that each
author's username and avatar are preserved.

## Commands

`/promote-link [name]` creates the channel, links back to the thread, and
replays nothing. This is the cheap path and the one to reach for first: three
API calls, no rate limiting, no fidelity loss, because nothing is copied.

`/promote-preview` reads the thread and shows what a replay would produce:
counts, oversized attachments, a rough run time, and the content rendered as an
HTML page. It creates nothing at all, not a channel, not a webhook, not one
message. Run this before `/promote`, always.

`/promote [name]` creates the channel and replays the entire history. Reach for
it when the content has to live in a real channel, for instance to attach an
integration that does not work on threads. Everything it emits is a webhook
copy, with the limits listed under "What is lost".

`/promote-abort` stops a running replay at the next message boundary. What was
already replayed stays, and `/promote-resume` continues from there. This is a
clean stop rather than a task cancellation, which would leave a half-emitted
message and no record of it.

`/promote-resume` continues an interrupted migration.

`/promote-verify` counts both sides and reports what is missing. Run it before
concluding that a migration went well.

`/promote-export` renders the migration manifest as an HTML page. Not an
archive of record: the source thread is never deleted, so Discord already holds
a better copy of the conversation than this page ever will, with its real
replies and reactions. What it is good for is looking at what the bot understood
of the thread, in one screen, without scrolling a channel. It earned its place
during testing by making two silent defects visible, an emptied manifest and a
duplicated opening post, and that remains its job.

`/promote-recover` re-opens the recovery choice for messages that failed during
a previous run.

`/promote-forget` clears the checkpoint without touching any channel.

All commands require the Manage Channels permission on the caller, and all of
them are guild only.

## Out of scope, by design

No message is posted in the source thread, and its state is never changed. The
bot only reads its history. Locking, archiving, deleting or announcing the move
is left to the server administrators.

The practical consequence is that a run costs nothing if the result is
unsatisfying: delete the target channel, run `/promote-forget`, and start again
against an untouched source.

Progress is reported by editing a single header message in the newly created
channel, which also carries the provenance of the replay once it is done.

## How resuming knows where to restart

Every replayed message carries a jump link naming the message it came from. A
resume reads the target channel back, finds the most recent of those links, and
continues after it. The channel is therefore its own record of what was done.

The checkpoint on disk is an optimisation, not the authority. Deleting it, or
finding it corrupted, costs one extra read of the channel and nothing else. This
is deliberate: a cursor that drifts out of step with reality is how a tool like
this replays a thousand messages twice.

For the same reason the target channel is resolved through the API and not
through the local cache, and only an explicit 404 is accepted as proof that it
is gone. A channel that merely cannot be reached stops the run with an error
rather than starting over.

Channel names are lowercased and hyphenated, because Discord normalises text
channel names that way regardless of what the API is given.

## Failed messages

A message that fails is first retried three times with growing backoff, which
absorbs the bulk of transient errors. If it still fails, a placeholder is posted
in its place, carrying the author, the avatar, the real timestamp and a link to
the original. The replay then carries on rather than stopping.

Because the placeholder holds the slot, the order of the conversation stays
intact and the failure can be repaired in place later, by editing that very
message rather than appending a correction at the end of the channel.

At the end of the run, every failure is listed in one report, with its position
in the channel, its author, its timestamp and the reason it failed. One choice
is then applied to the whole set:

Force means every failed message ends up present, degraded as far as necessary:
without attachments, then without embeds, then with truncated content, and as a
last resort the placeholder stands as a stub. Completeness first.

Best effort tries the full message, then without attachments, then without
embeds, and stops there. Anything still failing keeps its placeholder rather
than being silently truncated. Fidelity first.

Leave as is keeps every placeholder untouched. The gaps stay visible, the detail
stays in the manifest, and `/promote-recover` re-opens the choice later.

The prompt is deliberately a single decision for the whole set. A large thread
runs unattended for the better part of an hour, and asking per message would turn
a background job into a babysitting exercise.

## What is preserved

Each author's username and avatar, through the webhook `username` and
`avatar_url` overrides.

The full message text, split cleanly when a message exceeds 2000 characters once
annotations are added.

Attachments, downloaded and re-uploaded. Because CDN URLs are signed and
short-lived, the bytes are read immediately and never stored as a link. Spoiler
flags and alt text survive.

Stickers, re-sent as images since a webhook cannot emit them.

Original embeds, forwarded as-is up to ten per message.

Replies, remapped to the already-replayed copy in the new channel through an
old-id to new-id table. A webhook cannot produce a native reply, and a reply
never crosses a channel boundary anyway.

Reaction counts, as subtext under each message, with the full per-user list in
the manifest. The reactions themselves are not re-added by default, see
Limitations.

The real timestamp of every message, rendered as a dynamic timestamp in subtext,
with edited messages explicitly marked.

A jump link back to the original message, for as long as the thread exists.

Pins, restored after the replay.

The parent channel permissions, and for a private thread an explicit restriction
to its participants only.

## What is lost, and why

Real timestamps at the protocol level: a Discord snowflake encodes its own
creation time and cannot be forged. The subtext line is the best available
substitute.

The true author at the data model level: replayed messages are emitted by a
webhook and carry the APP badge. No API impersonates a user account, and that is
a good thing.

Per-user reactions: a bot cannot react on someone else's behalf. Counts and
names are kept as text and in the manifest.

Interactive components from other bots: buttons and select menus do not survive.

Custom emoji from other guilds, when the bot has no access to them.

Attachments larger than the target guild upload limit. They are listed in
subtext under the message concerned and flagged in the manifest. A local copy is
kept when `PROMOTER_KEEP_ATTACHMENT_COPIES` is on, so they can be republished
elsewhere, for instance behind a link to your own storage.

## Testing

    pip install pytest
    python -m pytest

The suite runs against a hand-written double of the discord.py surface, with no
network access. It includes a regression test asserting that no write method is
ever called on the source thread, and one asserting that a target channel absent
from the cache is never mistaken for a deleted one.

## Runtime

The practical ceiling sits around thirty messages per minute per channel for
webhook sends, and that bucket is shared with reactions and pins. At the default
two-second pacing, expect roughly thirty-five minutes per thousand messages, more
on threads heavy with attachments and reactions.

Those numbers are inherited from documentation, not measured here. discord.py
absorbs 429s silently, so a run that spent twenty minutes asleep looks like one
that never waited. The bot therefore counts the rate limits the library handles
on its behalf, shows them in the progress header, and records them in the
manifest. Two or three real runs will say whether two seconds is too cautious,
and the pacing should be changed on that evidence rather than on this
paragraph. The replay runs in the
background, reports progress in the source thread, and survives a restart thanks
to the checkpoint.

## Setup

Create the application in the Discord Developer Portal, then enable both
privileged intents in the Bot tab: Message Content Intent and Server Members
Intent. Without the former, message content comes back empty and the replay only
carries attachments.

Invite the bot with these permissions: View Channel, Read Message History, Send
Messages, Manage Channels, Manage Webhooks, Manage Messages, Add Reactions,
Attach Files, Embed Links.

Then:

    cp .env.example .env
    # fill in DISCORD_TOKEN and DISCORD_GUILD_IDS
    docker compose up -d --build

The compose file reads secrets through `${...}` substitution rather than an
`env_file:` directive. Substitution honours a file passed with `--env-file`,
which is what lets a deployment tool hand over a decrypted `.env`; `env_file:`
ignores it and injects the file into the container as it stands, encrypted or
not.

The container runs read-only as a fixed uid 1000, with a healthcheck watching a
heartbeat the bot refreshes while the gateway is up. A bot that stays connected
and stops making progress is the failure worth catching, and nothing else about
a gateway bot is observable from outside.

Setting `DISCORD_GUILD_IDS` syncs the commands immediately on the listed guilds.
Left empty, the sync is global and takes up to an hour to propagate.

## Persisted data

The `/data` volume holds four directories. `state` holds the checkpoints, one
JSON file per thread, including the id mapping table. `manifests` holds the full
archive of each migration, including everything that could not be replayed.
`attachments` holds local copies of the attachments. `exports` holds the HTML
archives produced by `/promote-export`.

These files are conversation content in the clear, plus a live webhook token for
as long as a run is in progress. Directories are created 0700 and files 0600,
and the replay webhook is deleted once a run completes cleanly, which is the
point at which the stored token stops being useful to anyone.

The volume is deliberately excluded from the backup jobs, and should be purged
once a migration has been validated. Nothing here is worth restoring
after the fact: the source thread is untouched and the run can simply be done
again.

## Operating notes

Prefer `/promote-link` unless something actually requires the messages to exist
in the new channel. The replay is the expensive, lossy path; the link is neither.

Test on a low-volume thread before touching a historical one.

Run `/promote-verify` before deciding a migration succeeded. It counts what is
really in the channel rather than what the run believed it did.

Check the result before doing anything to the source thread. As long as it
exists the jump links keep working, and a second pass remains possible.

Deleting the target channel is enough to start over: the next run notices the
channel is gone and resets the checkpoint, since the stored webhook and replay
cursor both belonged to that dead channel. `/promote-forget` remains available
for the case where you want to clear the state without deleting anything.

A guild is capped at five hundred channels, fifty per category. Promoting a
thread eats into that budget, which is exactly why `/promote-link` exists.
