// Opens the fixture page in headless Chromium and works through it: filters, split view, moved code, CI
// annotations, notes and ticks. Fails on any console error. Usage: node smoke.mjs PAGE.html [STACK.html]
import { chromium } from "playwright";
import { pathToFileURL } from "node:url";

const browser = await chromium.launch();
const errors = [];
let page;
async function open(file) {
  page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => { if (m.type() === "error" && !/fonts\.g|highlight\.js|cdnjs/.test(m.text())) errors.push(m.text()); });
  await page.goto(pathToFileURL(file).href);
}
const check = (ok, what) => { if (!ok) throw new Error(`failed: ${what}`); console.log(`ok   ${what}`); };
const count = sel => page.locator(sel).count();
const visible = sel => page.locator(sel).filter({ visible: true }).count();

await open(process.argv[2]);
await page.waitForSelector(".sec");
check(await visible(".sec") === 2, "two sections, with the formatting one hidden");
check(await count("td.noise") === 0, "formatting lines inside a hunk show once, as context");
await page.check(".fmtchk input");
check(await visible(".sec") === 3 && await count("td.noise") > 0, "the formatting switch shows them, dimmed, and their section");
await page.uncheck(".fmtchk input");
check(await visible(".sec") === 2 && await count("td.noise") === 0, "and hides them again");
check(await count("tr.mv") === 2, "a moved block shows where it went and where it came from");
check(await count("td.code.moved") === 6, "the moved copy is marked line by line");
check(await count("tr.ann") === 1, "the CI annotation on a changed line sits under it");
const ready = await page.locator(".ready").innerText();
for (const want of ["Try by hand", "What the diff carries out (1)", "Not in the diff:", "CI annotations", "1 on lines this PR doesn't change", "new since"])
  check(ready.includes(want), `ready panel has "${want}"`);
check(!(await page.locator(".ready details", { hasText: "Description vs diff" }).innerText()).includes("served under /blog"),
  "the description section lists only where it and the diff disagree");

const options = await page.locator(".pick option").allInnerTexts();
check(options.length === 6 && options[1].startsWith("New since"), "the filter lists the last pass and the four commits that own hunks");
await page.selectOption(".pick", "fresh");
check(await visible(".card") === 1 && await visible(".sec") === 1, "since the last pass, only the file that changed shows, in its section");
await page.selectOption(".pick", "c1");
check(await visible(".card") === 1 && (await page.locator(".card:visible .bn").innerText()) === "posts.ts", "one commit shows only its file");
await page.click(".filtering button");
check(await visible(".card") === 5, "Show everything brings every card back");
// Only when highlight.js loaded from its CDN: a .bru line inside a tests block is highlighted as JavaScript.
if (await page.evaluate(() => !!window.hljs)) {
  const line = page.locator(".card", { hasText: "get-post.bru" }).locator("td.code.add", { hasText: "Hello World" });
  check(await line.locator(".hljs-string").count() === 1, "a .bru test line is highlighted as JavaScript");
}

const line = page.locator('td.ln[id$="-R5"]').first();
await line.hover();
await line.locator(".cbtn").click();
await page.fill("tr.composer textarea", "Why /blog and not /posts?");
await page.click('tr.composer [data-act="done"]');
check(await count("tr.note") === 1, "a note shows under its line");

await page.click('.seg button[data-view="split"]');
check(await count("table.d:not(.split)") === 0, "split view draws every diff in two halves");
check(await count("tr.note") === 1, "the note finds its line again after the redraw");
const paired = await page.locator("table.d.split tr.line.chg").evaluateAll(rows => rows.some(r => r.querySelector("td.del") && r.querySelector("td.add")));
check(paired, "a removed and an added line share a row");
await page.locator("tr.mv button").first().click();
check(await count("td.code.del") >= 6, "the moved-away lines open from their pointer");

const box = page.locator(".card .chk input").first();
await box.check();
await page.reload();
await page.waitForSelector(".sec");
check(await page.locator(".card .chk input").first().isChecked(), "a tick survives a reload");
check(await count("table.d:not(.split)") === 0, "the layout choice survives a reload");

console.log("page ok");

// The stack page: two layers, one shown at a time, each drawn by the same code.
if (process.argv[3]) {
  await open(process.argv[3]);
  await page.waitForSelector(".layers");
  check(await page.locator(".layer:not([hidden]) h1").innerText() === "layer 12", "the stack opens on its top layer");
  check(await count("tr.xref") >= 1, "a hunk names what it shares with another layer");
  await page.click('.layer:not([hidden]) .seg button[data-view="split"]');
  check(await page.locator(".layer").evaluateAll(ls => ls.every(l => !l.querySelector("table.d:not(.split)"))), "split view redraws every layer");
  await page.click('button.lrow[data-n="11"]');
  check(await page.locator(".layer:not([hidden]) h1").innerText() === "layer 11", "a layer opens from the list");
  console.log("stack page ok");
}
await browser.close();
if (errors.length) { console.error(errors.join("\n")); process.exit(1); }
