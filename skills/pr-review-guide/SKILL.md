---
name: pr-review-guide
description: Use when the user wants to self-review a PR or branch section by section rather than file by file, asks for a review guide, guided review, "Linear-style guide", or a page that splits a diff into logical sections with explanations, line-count breakdown and notes on lines, including one page for a whole stack of PRs. Also use when the user says to work through, address or act on the notes or comments they left on a review guide page, or when a review guide page sends its notes to Claude.
---

# PR review guide

A page that does what Linear's Guide tab does for a PR, built locally: the diff split into logical sections, each with why it exists and how it works, formatting and lock or generated files kept out of the way, and notes on lines that come back to this session. It is for reviewing your own work before someone else does, and it opens with what to settle before marking the PR ready: size, CI, missing tests, lines that look like an agent's leftovers, where the description and linked issue disagree with the diff, which sections need a careful read, a few things to try by hand, and whether the PR should be several. Rebuilt after a round of fixes, it shows what changed since the last build and keeps the reviewed ticks on everything else.

Build it last, on the diff that will merge: after the agent's work is done and after `/code-review` and `/simplify`, whose fixes are themselves worth reading. The scripts do everything mechanical; Haiku digests large diffs, Opus writes and checks the sections, and the main session publishes and later acts on the notes.

Three reference files are for when they're needed, not every build: `reference/page.md` for what the page shows, `reference/collect.md` for why a line was flagged, hidden or shown as moved and how issues are linked, and `reference/design-notes.md` for why the models and steps are what they are.

## What it needs

- A local clone of the repo and `git`. No issue tracker access (Linear, Jira or GitHub issues), no checkout of the PR branch (the PR's commits are fetched by ref).
- [`gh`](https://cli.github.com) logged in, for a PR by number, its CI and CI annotations, and its linked GitHub issues, and for a stack of PRs. Without it, a branch compared with `--base` or against the remote's default branch needs only `git`, so it works on any host.
- `python3`, standard library only.
- The Artifact tool and its `comments` capability, to publish the page and have notes sent back. If you run more than one Claude account, publish from the one your team can see: an artifact belongs to the account that published it. Without the Artifact tool, open the rendered file in a browser instead: notes and Copy notes work there, Send to Claude doesn't, and the page can't be shared.

## Building a guide

Work in a fresh directory under the scratchpad, `WORK` below. `SCRIPTS` is this skill's `scripts/`.

1. Collect. For a PR: `python3 SCRIPTS/collect.py --pr N --repo PATH --out WORK`. For a branch with no PR yet, give `--head BRANCH` (default `HEAD`) and optionally `--base REF`; without one it takes the branch's PR base, then `origin/HEAD`, then `origin/main`. It prints the line breakdown and how the reviewable size reads, lines that only moved, what is new since the last build, CI and its annotations, whether tests changed and any flagged lines. It writes the PR description and commit messages to `WORK/description.md`, and the GitHub issues the PR links to, under the repo's own owner only, to `WORK/issue.md`. Jira or Linear keys it lists as not fetched, each marked as closed by the PR or related: fetch each one's title and description with whichever tracker tool this session has (a Linear or Jira MCP server or CLI) and add it to `WORK/issue.md` as `## KEY: title`, a line saying "This PR closes it." or "Linked from this PR, which doesn't close it.", then the text. Write an issue once when two keys turn out to be the same one (a moved issue keeps its old key), and skip any you can't reach. At 0 reviewable lines the change is all formatting, lock and generated files: write `{"sections": []}` to `WORK/guide.json` and go to step 7, unless it also lists files with no line changes (binary files, renames, mode changes), which still need a section.

2. Digest, only above about 1,200 reviewable lines. Spawn one Agent per `WORK/batches/batch-N.diff`, all in one message, with `model: "haiku"` and the prompt in `reference/digest-prompt.md`. Each writes `WORK/digests/batch-N.json`.

3. Guide, and the description check alongside it. In one message, spawn two Agents, both `model: "opus"`: the guide worker with `effort: "medium"` and `reference/guide-prompt.md`, its `{SOURCE}` line chosen by whether step 2 ran, and the description checker with `reference/description-prompt.md` at default effort. The checker reads the diff before the description, so the author's account can't steer it, and writes `WORK/description.json`. The guide worker writes `WORK/guide.json`, with risk notes, things to try by hand and, on a long PR, a suggested split, and runs the check itself.

4. Check. Spawn a fresh Agent, `model: "opus"`, `effort: "medium"`, with `reference/verify-prompt.md`, giving it the clone's path and the head sha from `hunks.json`. It didn't write the guide, which is the point: it goes hunk by hunk and sentence by sentence against the diff and writes `WORK/verify.json`, moves and corrected sentences it can justify, not style. It judges from the diff and the changed files at the head commit only, and reports anything it can't confirm from those as unconfirmed.

5. Apply. Send the guide worker the message at the end of `reference/guide-prompt.md`. It applies the issues it agrees with and lists the rest, with a reason each, in `WORK/disputes.json`. If it reports back without a passing check, send it the output of `render.py WORK --check` once; if that fails too, fix `guide.json` yourself rather than looping.

6. Read `disputes.json` and decide each one yourself against the diff: the worker and the checker disagree, and you are the tie-break. Then read the section titles and text once more. `description.json` is not for the guide: it is for the author's PR description, and goes on the page as is. Edit `guide.json` directly and re-run the check for anything you change.

7. Render: `python3 SCRIPTS/render.py WORK --out SCRATCHPAD/<repo>-<n>-guide.html`.

8. Publish. With no Artifact tool in this session, open the rendered file in the user's browser (`open` on macOS, `xdg-open` on Linux) and say that Copy notes is how their notes get back here; skip the rest of this step. Otherwise look the PR up in `~/.claude/pr-review-guide/artifacts.json` (`{"owner/repo#N": url}`, or `owner/repo@branch` for a branch) first.
   - Not there: publish with the Artifact tool, that file path, `icon: "code"`, a one-line description naming the PR and `capabilities: {"comments": {}}`, then add the URL to the file.
   - There, and published from this session: publish the same file path again.
   - There, from an earlier session: read the artifact with `action: "read"`, then publish the new file with `url` set to it. Without `url` the publish makes a second page, and the review threads stay behind on the first.

Hand over the link with the section titles, the line breakdown, one line per dispute you settled and which way, and the ready check in a sentence or two: flagged lines, description mismatches, risky sections and any suggested split. Those are what the author fixes before marking the PR ready; fix none of them yourself unless asked.

## A stack of PRs

For a stack, where each PR is based on the one below it as [gh-stack](https://github.com/github/gh-stack), [Graphite](https://graphite.dev) or hand-made stacks leave them, one page holds every layer. It needs `gh` and nothing else: the layers come from the open PRs' base branches, not from any stacking tool's metadata. `SCRATCHPAD` and `SCRIPTS` are as above; `STACK` is a fresh directory.

1. Collect: `python3 SCRIPTS/stack.py --pr N --repo PATH --out STACK`, where N is any layer. It finds the other layers from the PRs' bases, runs `collect.py` once per layer into `STACK/<pr>/`, and writes `STACK/stack.json`: the layers bottom to top, the cross-layer links, churn (lines a layer adds that a later one removes again) and which layers need a rebase. Where two open PRs share a base further up, it stops there and says so, and you pass the top PR of the branch you want.
2. Steps 2 to 6 above for every layer at once, with `WORK` being `STACK/<pr>`: every layer's digest workers in one message, then every guide worker and description checker in one message, and each checker as soon as its guide is in. A layer whose head has not moved since you built its guide earlier in this session can reuse that guide's `guide.json`, `verify.json`, `description.json` and `disputes.json`: the same base and head give the same hunk ids, which `cmp` on the two `review.diff` files confirms.
3. The stack check, the last step before rendering. Spawn one Agent, `model: "opus"` at default effort, with `reference/stack-prompt.md`. It writes `STACK/stack-guide.json`: what each layer does in the stack, and the issues only the whole stack shows, such as code a later layer rewrites, a hunk that belongs in another layer, or a layer that cannot merge without a later one. It also edits the guides: a clause linking into another layer where that explains a section, as `[phrase](1013:f42h1)`, a sentence another layer contradicts, and a `stack` note on a description finding another layer explains. Its `edits` list says what it changed; read that and the summaries once, as you read the disputes, and run `render.py STACK --check` after anything you change.
4. Render: `python3 SCRIPTS/render.py STACK --out SCRATCHPAD/<repo>-<top>-stack.html`. It checks every layer as `--check` does, and every link into another layer.
5. Publish as step 8 does, keyed `owner/repo#N stack` for each layer N. Look up every layer's key and use the first URL found, then record every layer's key against the page. Rebuilding from any layer then finds it, and a layer that merges and drops out of the stack does not start a new page.

Hand over the link with one line per layer, its summary and its ready check in brief, then the stack's issues and the edits the stack check made.

## Working through the notes

The usual setup is this session in a terminal, watching the page, and the page open in a browser tab. The user leaves notes on lines, kept in their browser until they press Send N notes to Claude. That posts one comment holding every note and sends it to whichever session is watching the page (the one that published it, or one that watches it with `ArtifactComments`), and it arrives here as a turn. The report goes in this session, since that is where the user wants it; the platform may already have posted a short automatic reply on the page, so leave that.

Read the thread with `ArtifactComments` `action: "read"`; a long review comes in parts, the first saying how many, so read again until all of them are there. Or the user pressed Copy notes and pasted the same text into this session as their own message, with no thread on the page and no automatic reply. Then:

1. Each note opens with its location: `path:R<n>@<sha>` for a line in the new file, `path:L<n>@<sha>` for a removed line numbered in the base, prefixed with the layer on a stack page, `#1014 src/store/auth.ts:R42@c3e8b96`. The sha is the head the page was built from; if the branch has moved since, find the line by content rather than trusting the number. A thread left through the shell's own comment mode carries no location, so ask which line it means rather than guessing.
2. Treat each note as the user's review note, and the code as the source of truth. Make the change in the working tree and leave it unstaged; if a note is a question rather than an ask, answer it in the report instead of changing code. On a stack, fix only the layer whose branch is checked out and list the rest by layer, since uncommitted fixes cannot follow a checkout and restacking is the user's.
3. Report in the session, one line per note: the location and what you did or why you didn't. If the notes came as a comment, resolve that thread once done; any thread not sent to Claude stays open for the user to resolve, so say which.
4. Rebuild once the fixes are committed or pushed, from step 1; step 8 finds the existing page and updates it. The page then marks the hunks that are new or changed since the last build and offers them as a view of their own, and the reviewed ticks stay on files whose hunks didn't change.

## Common mistakes

- Running the check in the guide worker's own context, or skipping it because `--check` passed. Coverage only proves every hunk is somewhere; the checker is what catches a hunk in the wrong section or a sentence the code does not bear out.
- Running the digest step on a small diff. It costs a round of workers and the guide comes out vaguer, because the guide worker then reads summaries instead of code.
- Asking the guide worker to review the code. It describes and groups; review is the user's job on the page, and their notes are where findings come from.
