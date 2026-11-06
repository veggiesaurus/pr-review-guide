# What collect.py decides

Read this when the user asks why a line was flagged, hidden as formatting or shown as moved, why an issue was or wasn't linked, or before changing `collect.py`. Everything here is mechanical; no model is involved.

## Size

The reviewable lines are the changed lines left once formatting, lock and generated files and the removed side of moved code are taken out. With the guide, up to 800 fits one sitting and over 2,000 is too many; the bands are `SIZE_BANDS` in `collect.py`, and from the second one the guide suggests a split.

## What gets flagged

A short list of lines a reviewer should not have to find, on implementation and test files only:

- in tests: a skipped or focused test (Jest, Mocha, pytest, Go, Rust, JUnit and RSpec), a removed test, an assertion removed or changed (changed expected values are fine when behaviour was meant to change, but worth a look), a deleted test file
- anywhere: debug output (`console.log`, `debugger`, `breakpoint()`, `pdb`, `dbg!`, `binding.pry`, `byebug`), a silenced check (`@ts-ignore`, `eslint-disable`, `# noqa`, `# type: ignore`, `//nolint`, `#[allow(...)]`, `rubocop:disable`, `@SuppressWarnings` and the like), `any` in JS and TS, a new TODO, commented-out code

Lines that only moved or were re-indented are never flagged, since they were already there. The list is narrow on purpose. Every published account of AI review says noise, not missed bugs, is what makes people stop reading flags, so add a rule only when it is nearly always worth a look.

## What counts as noise

- whitespace: a block that is the same once layout whitespace outside string literals is removed, which covers re-indenting and re-wrapping. A space between two words stays, since `not ready` isn't `notready`, and in shell scripts, Dockerfiles, YAML and other config every gap between tokens stays, since `rm -rf /tmp/ x` isn't `rm -rf /tmp/x`. In Python, YAML, Makefiles and the other indentation-sensitive languages, each line's indentation has to match too, so a dedent or a re-nesting stays a change.
- re-indented: inside a real change, a line that `git diff -w` does not call changed, which is how GitHub's hide-whitespace view counts it, and that appears on the other side with the same words (`-w` also ignores a space dropped between words). Removing an `if` and dedenting its body leaves only the `if` as a change: the body is counted as formatting and shown once, as context, to the guide workers, and on the page unless formatting is shown. In indentation-sensitive languages this needs the same hunk to change a line opening that level, at the depth the lines moved to or from, so a statement quietly moved out of a block stays a change even when a function signature above it changed too.
- formatting: the same once quote style is ignored (JS, TS and Python), and semicolons, including dropping them at the ends of lines, trailing commas and parentheses around a single arrow parameter (JS and TS only, where Prettier settles them). Template literals, SQL quotes and Python's trailing commas mean something, so they are left alone, and so does a line break between two words in JS and TS, so `return;` then `foo()` isn't `return foo()`.
- reordered: imports in a different order. Any other reordered lines are a change.
- moved imports: imports removed in one place and added unchanged elsewhere in the file.
- lock and generated files: by name (lock files, `*.gen.*`, `*_pb2.py`, snapshots, minified bundles, top-level `dist/`, `build/` and `vendor/`, `node_modules/`), by a "generated, do not edit" header, or by `.gitattributes` (`linguist-generated`, `review-generated`, and `-linguist-generated` to take a path back out)

A rename is not noise: it changes what the code says, so it stays in its section. `.gitattributes` also takes Linear's `review-test`, `review-documentation` and the rest for categories, including the `-review-test` form to take a path out of one.

## Moved code

Other moved code stays a change, since the same line can mean something else in another function, but a run of at least three lines (and 40 letters and digits, so `if err != nil { return err }` doesn't count) that comes back unchanged elsewhere in the diff is shown as a move, and the guide workers read it as one. Lines match with their indentation ignored; in Python, YAML and the other indentation-sensitive languages they have to keep their indentation relative to each other. A block moved with edits shows as moved runs around the edited lines.

## Since the last build

Each build records its head and a signature for every hunk, which is what the hunk changes without where it sits, in the clone's git directory (`.git/pr-review-guide/passes/`, the last five per PR or branch). The next build at a different head marks hunks whose signature is new, so a rebase that only moves the change leaves a hunk as it was, and the page keys its ticks the same way.

## Linked issues

Links are read the way GitHub and Linear read them: closing words (`Fixes`, `Closes`, `Resolves`, and Linear's `Completes` and `Implements`, which close only a Linear or Jira issue), linking words that don't close (`Part of`, `Refs`, `Related to`, `Contributes to`), `skip` or `ignore` to leave one out, issue and tracker URLs, and a key in the title or branch name, which closes it as Linear does. GitHub issues are fetched into `issue.md` only under the repo's own owner, since the PR's author writes the links and the reviewer's `gh` login may read more than they can. Jira and Linear keys are left for the session to fetch with its own tracker tool.

## Commits

Each hunk's commits come from `git blame` over the PR's range, and for removed lines from blaming the base in reverse: a line last seen in one commit was removed by the next. It is skipped past 150 files.

## CI annotations

The warnings and failures the head's check runs pin to a line, fetched with `gh api` when CI ran on the same head as the diff. Actions pins its own notices ("Node.js 20 is deprecated") to `.github`, which is not a file, so those are dropped. A check only leaves line annotations when its tool reports in a form GitHub reads: a step printing `::error file=...,line=...::`, a problem matcher, or an action that writes them itself, such as golangci-lint's.
