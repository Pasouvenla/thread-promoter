# Contributing

## Running the tests

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt pytest ruff
    python -m pytest
    ruff check app tests

The suite runs against a hand-written double of the discord.py surface and
needs no network access and no bot token.

## What a change needs

**A test that fails without it.** For a bug fix, write the test first and watch
it go red: a test that cannot fail proves nothing. Several of the defects this
project has had were invisible to a green suite.

**A reason in the commit message, not a description of the diff.** The diff is
already in the commit. What is worth writing down is why the old behaviour was
wrong and what breaks if someone reverts it.

## Two rules that are not negotiable

**The source thread is read only.** Nothing is written to it, its state is never
changed, it is never locked, archived or deleted. This is what makes an
unsatisfying run repeatable: delete the target channel and start again against
an intact source. A test asserts that no write method is ever called on it.

**No silent loss.** If something cannot be carried over, it is stated under the
message concerned and recorded in the manifest. A message that looks complete
while missing content is worse than a visible gap.

## Style

English throughout: identifiers, comments, docstrings, user-facing strings,
documentation and commit messages. No emoji anywhere. Comments explain why, not
what.

No new runtime dependency without a reason in the pull request. The bot runs on
discord.py and aiohttp, and that is meant to stay true.
