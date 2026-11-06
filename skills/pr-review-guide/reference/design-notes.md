# Design notes

Why the pipeline is the shape it is, for anyone changing the models, efforts or steps. None of this is needed to build a guide.

- Opus writes the sections. In a side-by-side on three PRs it split files across sections by purpose where Sonnet kept them whole, and Sonnet's grouping changed between runs on the same diff. It costs 10 to 15 seconds more. Haiku was as fast as Opus on the digests and good enough, so it stays there.
- The description checker runs at default effort, not medium: at medium it found one of two omissions on a test PR, at default both, in 53 seconds against the guide's 37, and it runs alongside the guide anyway.
- The checker and the guide workers judge from the diff and the changed files, not the repository. Steps 3 to 5 take about two minutes on a 664-line PR: under a minute for the guide and the description check side by side, a minute for the check, a few seconds to apply. With every worker at high effort and the checker free to search the repository, the same PR took over seven, for guides of much the same quality.
- The stack check is the slowest step: six minutes on a four-layer stack of about 3,900 reviewable lines, against about four for all the layers before it.
- The digest step only runs above about 1,200 reviewable lines. Below that the guide worker reads the diff and does better for it.

## Tests

`collect.py --self-test` and `render.py --self-test` check the normaliser, the categories, the diff parser, the hunk connections, moves, issue links, the coverage rules and the warnings. `tests/fixture.py` builds a small repo with a branch, collects it twice and renders a page and a two-layer stack page, and `tests/smoke.mjs` opens both in headless Chromium and works through the filters, split view, formatting, moves, notes and ticks. CI runs both.
