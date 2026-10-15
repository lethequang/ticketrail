# How grouping works

The goal: every session of one task or problem sits in one work item, even when the work spanned several agents, days and
roles: research, code, test, docs.

## One work item per session

For each session the first rule that applies decides the work item:

1. **Manual override.** You moved the session (`m`) or merged its work item into another (`g`).
2. **Ticket in the name you gave the session.** For example `/rename SHOP-142 checkout retry` in Claude Code, or a
   Codex thread name.
3. **Ticket in the title.** Among the tickets the session mentions, one that also appears in its title wins. The title
   is the name you gave the session, else the AI-generated title. This is deliberate: the title states what the
   session is about, while hit counts are noisy.
4. **Ticket of the git branch, if the session mentions it.** For example `feature/ABC-142-export`. A branch can outlive
   its ticket, so it never overrides what the session talks about. A session that went through branches of different
   tickets says nothing reliable through its branch, so its branch is ignored.
5. **Ticket mentioned most.** Mentions are counted in your prompts and in the names of files the session wrote. A link
   to the configured tracker counts once more.
6. **Ticket of the git branch, for a silent session.** This applies to a session that mentions no ticket at all.
   - Branch matching uses your ticket patterns as they are, so it is case-sensitive.
   - With the default pattern, a lowercase branch such as `eng/abc-12-x` yields no ticket.
   - GitHub `#123` keys never appear in branch names.
7. **LLM topic.** The labeler names the problem with a short slug and reuses existing slugs for the same problem.
   - If at least two sessions with a topic carry tickets and at least two thirds of them share one ticket, ticketless
     sessions of that topic join that ticket's work item.
   - Otherwise the topic is a work item of its own (`◆`).
8. **Unsorted.**

Overrides are stored by full session id. Codex ids are time-ordered, so two sessions opened seconds apart share their
first 8 characters; storing the full id keeps the other session from being moved by mistake.

## Related is not grouped

Two things mark a session as *related* without moving it:

- **secondary tickets:** tickets it mentions besides its work item's;
- **shared files:** files written by 2 to `related.hub_max` sessions in other work items.

Files written by many sessions, such as a shared helper or a changelog every session appends to, are hubs. A hub
relates nothing: early versions that merged work items through shared files put a third of all sessions into one work item.

## Group title

The title is chosen in this order:

1. your rename;
2. for a ticket work item, the rest of a session name that contains the ticket;
3. the most common LLM topic title;
4. the newest session's title.

## Key session

A work item's key session (`⚑`) is the one you pinned with `p`. Without a pin, it is the session that left the most files
(scratch files and plans don't count); ties go to more prompts, then to the newer session. A pin that points at a
session you later moved away is ignored.

## Labels

The labeler runs in the background for sessions idle for 10 minutes. It writes a label with these fields:

- `topic`;
- `title`;
- `role`: research, code, test, docs, ops, env, or Q&A;
- `status`: done, in progress, waiting on others, or abandoned;
- `summary`;
- `next`: what is left.

A session is labeled again after 5 more prompts. A label update that is not caused by such growth, such as an upgrade
of the label format, keeps the existing topic so that work items do not reshuffle. A failed call keeps the previous label
and is retried an hour later. After 3 failures in a row the run stops, so a broken CLI does not mark the whole queue.

The status is the model's judgement from the last prompt and the final answer. It is shown as such ("judged by LLM")
and an unknown value is shown as `?`, never guessed.

## Status of a work item

A work item shows the status of its newest session, whatever the filters on screen.

When `github.pull_requests` is on, the item is **done** if all of these hold:
- the item's branches have pull requests, and they are all merged or closed;
- at least one of them is merged;
- no session of the item was active after the merge (work that resumed after a merge is not done).

The merged PR is then shown as the reason instead of the model's judgement. An open PR keeps the model's status, and the
PR is listed among the links.

A pull request is not matched to a session in these cases:
- it comes from a fork;
- its branch is the base of other pull requests (`develop`, `main`);
- its branch names a ticket of another work item.

## Live dot

`●` means an agent is working; `◐` means a Claude session is open and waiting for you.
- **Claude sessions:** the state comes from `claude agents --json` (Claude Code ≥ 2.1.139). When that command is
  missing or fails, a transcript changed in the last 3 minutes counts as working.
- **Codex sessions:** always use that transcript rule.
