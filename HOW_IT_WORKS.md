# How This Code Works — Beginner's Guide

You don't need to understand every line to use this project. But if you want
to learn, this guide walks through the whole thing in plain language.

## The big idea (one paragraph)

The program keeps a "photo" (snapshot) of what every website said last time it
looked. Each run, it takes a fresh photo and compares: *did any new sentence
appear that wasn't there before?* If yes, and that sentence contains a word
you care about (like "scholarship" or "deadline"), it sends you an alert.
That's it. Everything else is detail.

## The files

```
watcher.py        ← the main program (the "engine")
nsp_adapter.py    ← special expert that only understands NSP's scheme list
sites.json        ← YOUR settings: which sites, which keywords
state/            ← the saved "photos" (one JSON file per site)
alerts.log        ← every alert ever sent, kept forever
HOW_IT_WORKS.md   ← this file
```

## watcher.py, step by step

The program runs top to bottom like this:

### Step 1 — read your settings
`load_config()` opens `sites.json` and reads the site list and keywords.
**This is the only file you ever need to edit.**

### Step 2 — download each page
`fetch()` downloads a page the same way your browser does. Details:
- It sends a "User-Agent" — a label saying "I am a Chrome browser" — because
  some sites refuse visitors that look like robots.
- If the download fails, it waits 5 seconds and tries once more
  (government sites often hiccup for a moment).
- A few sites need `fetch_browser()` instead: it opens a real invisible
  Chrome window (called a "headless browser") because those sites build
  their content with JavaScript, which a plain download can't see.

### Step 3 — turn the page into clean text lines
`extract_lines()` does the cleanup:
- A web page is HTML — text wrapped in tags like `<div>` and `<p>`.
  BeautifulSoup (a library) strips the tags and keeps the words.
- We delete `<script>` and `<style>` blocks (code, not content).
- We throw away "noise": visitor counters, clocks, copyright lines —
  things that change constantly but mean nothing.
- Result: a simple list of sentences, like
  `["Apply For Scholarship", "Last date: 31-10-2026", ...]`

### Step 4 — compare with last time
`check_site()` loads the previous snapshot from `state/` and asks:
*which lines are new?* It's a simple set comparison —
"lines in today's photo that were not in yesterday's photo."

The **first ever run** has no old photo, so it just saves one and stays
silent. That's called the *baseline*. Alerts begin from the second run.

### Step 5 — filter by keywords
A new line only becomes an alert if it contains one of your keywords
(`"scholarship"`, `"internship"`, `"deadline"`...). This is the spam filter:
sites change small things daily, but you only hear about relevant changes.

### Step 6 — send the alert
`alert()` sends the message through every channel you configured:
- **Telegram** — `send_telegram()` calls Telegram's bot API (one web request)
- **Email** — `send_email()` logs into your mail provider and sends a mail
- **alerts.log** — ALWAYS written, even if both channels fail,
  so no alert is ever lost

### Step 7 — report health
At the end, the program prints a summary: how many sites checked, alerted,
failed. If lots of sites fail at once (usually means internet trouble or
many sites blocking), it warns you on Telegram/email too.

## nsp_adapter.py — the NSP expert

The generic engine says "*something* changed on NSP." The adapter is smarter:
it reads NSP's All-Scholarships page and builds a record for every scheme:

```
name:     "AICTE Pragati Scholarship For Girl Students"
ministry: "All India Council For Technical Education"
status:   "OPEN"        ← or "CLOSED" or "NOT YET OPENED"
deadline: "31-10-2026"
```

Then it compares records field by field, so it can say exactly:
- 🟢 this scheme just OPENED (the "it's live!" alert)
- 🔴 this one closed
- 📅 this deadline moved
- 🆕 brand-new scheme appeared
- ❌ a scheme was removed

It also protects itself: if it suddenly finds fewer than 5 schemes, it
assumes NSP changed their page design and reports *itself* as broken
instead of sending false alerts.

## Things that confuse beginners (quick answers)

- **What is JSON?** A simple text format for structured data:
  `{"name": "NSP", "url": "https://..."}`. Both `sites.json` and the
  snapshot files use it. Python reads/writes it with the `json` library.
- **What is an environment variable?** A named value (like a password) that
  lives *outside* the code, so secrets never get written into files you
  might share. `os.environ.get("EMAIL_APP_PASSWORD")` reads one.
- **What is `state/`?** The program's memory between runs. Delete it and the
  program forgets everything and starts fresh with new baselines (that's
  safe — you just get no alerts on the very next run).
- **What is GitHub Actions?** A free service that runs your code on GitHub's
  computers on a schedule (ours: every 3 hours), so your own PC can be off.

## If something breaks

1. Run `python watcher.py` and read the output — failures are listed by name.
2. One site failing = usually that site's problem; it often fixes itself.
3. Many sites failing = check your internet, or the run summary message.
4. "only parsed N schemes" from the NSP adapter = NSP redesigned their page;
   the parser in `nsp_adapter.py` needs updating (ask Claude to re-probe it).
