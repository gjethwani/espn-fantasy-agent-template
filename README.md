# ESPN Fantasy Agent

An autonomous general manager for your ESPN fantasy football team, built on
[Claude Code Action](https://github.com/anthropics/claude-code-action) and
GitHub Actions. It reads your real league data straight from ESPN, sets your
lineup, works the waiver wire, and proposes/responds to trades — daily, on a
schedule, with no human in the loop by default.

## What's in here

| File | What it does |
|---|---|
| `espn_actions.py` / `espn-act` | CLI that reads your league (`show`, `find`) and makes moves (`set-lineup`, `add-drop`, `waiver`, `trade-propose`, `trade-respond`). Every write is a dry run unless you pass `--execute`. |
| `.github/workflows/espn-fantasy-check.yml` | **The GM.** Runs daily (default 13:00 UTC). Reads the full league snapshot, evaluates your lineup/roster/trades, executes what helps, and reports on a GitHub issue titled "Fantasy check - \<date\>". **Its `prompt:` is blank — see [Write your prompts](#write-your-prompts) below.** |
| `.github/workflows/espn-fantasy-poll.yml` | **The watchdog.** Runs hourly with no LLM cost — just ESPN reads, no prompt to write. If something meaningful changes between daily checks (a starter's injury status, a bench player suddenly out-projecting a starter, a tracked waiver player clearing, a new trade offer, etc.), it triggers an early, out-of-schedule run of the workflow above instead of waiting. See the `cmd_poll` docstring in `espn_actions.py` for the full, exhaustive list of trigger conditions. |
| `.github/workflows/espn-fantasy-ask.yml` | **Manual Q&A.** Trigger it from the Actions tab with a free-text question ("should I trade X for Y?", "who do I start at flex?"). Meant to be read-only — never executes a roster move — and prints its answer both to the job's own step log (mobile-friendly) and the run summary. **Its `prompt:` is also blank; see below.** |

## Setup

### 1. Use this template

Click **Use this template** (or fork/clone) to get your own copy.

### 2. Get your ESPN credentials

ESPN's fantasy API isn't public, so this authenticates the same way your
browser does, via cookies:

1. Log into [fantasy.espn.com](https://fantasy.espn.com) in a browser.
2. Open DevTools → **Application** (Chrome) or **Storage** (Firefox) →
   Cookies → `https://fantasy.espn.com`.
3. Copy the values of two cookies:
   - `espn_s2` — a long string
   - `SWID` — looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}` — **keep the
     curly braces**, they're part of the value

If your league is private, these are required. If it's public, they still
don't hurt.

### 3. Find your league ID and team ID

Open your team on ESPN and read them out of the URL:

```
https://fantasy.espn.com/football/team?leagueId=XXXXXXX&teamId=Y
```

### 4. Get an Anthropic API key

Create one at [console.anthropic.com](https://console.anthropic.com) — this
is what pays for and authenticates Claude's calls in
`espn-fantasy-check.yml` and `espn-fantasy-ask.yml`.

### 5. Add repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**
and add each of these:

| Secret | Value |
|---|---|
| `ESPN_S2` | The `espn_s2` cookie value |
| `ESPN_SWID` | The `SWID` cookie value, including `{ }` |
| `ESPN_LEAGUE_ID` | Your league ID |
| `ESPN_TEAM_ID` | Your team ID |
| `ESPN_SEASON_YEAR` | The season year, e.g. `2026` — update this every season |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

### 6. Write your prompts

**This template ships with no prompts.** `prompt: ""` in both
`espn-fantasy-check.yml` and `espn-fantasy-ask.yml` is intentionally blank —
the prompt *is* the strategy, and that's the one part of this you have to
write yourself. Until you fill these in, the workflows will run but Claude
will have no instructions to act on.

**`.github/workflows/espn-fantasy-check.yml`**, under the "Run Claude Code"
step's `prompt:` key. This is the autonomous GM — cover at minimum:
- **The objective.** What "winning" means to you and how aggressive to be.
- **The tools available**: `./espn-data.json` (the full snapshot — roster,
  matchup, free agents, `acquisition_settings`, `pending_trades`,
  `bye_weeks`, per-player `wire_status`/`bye_week`) and `./espn-act`'s
  subcommands (`show`, `find`, `set-lineup`, `add-drop`, `waiver`,
  `trade-propose`, `trade-respond` — run `./espn-act --help`). Every
  mutating subcommand dry-runs unless `--execute` is passed; tell Claude to
  dry-run first, read the payload, then execute.
- **Acquisition strategy.** Read `acquisition_settings.isUsingAcquisitionBudget`
  to tell FAAB leagues from priority-waiver leagues and instruct different
  behavior for each — don't hardcode an assumption about your league's rules.
- **Decision rules** for lineup construction, when to stream/hold a
  bench spot, and what makes a trade worth proposing or accepting.
- **How to report.** The original implementation filed a GitHub issue per
  day (`gh issue create`/`gh issue comment`) — pick whatever you want.
- **(Optional) special-run context.** If you enable `espn-fantasy-poll.yml`,
  it dispatches this workflow with a `reason` input describing what changed.
  Reference `${{ github.event.inputs.reason }}` somewhere in your prompt so
  a special run knows why it was woken up, e.g.:
  ```yaml
  prompt: |
    Your instructions here.

    ${{ github.event.inputs.reason != '' && format('This run was triggered
    early because: {0}', github.event.inputs.reason) || '' }}
  ```

**`.github/workflows/espn-fantasy-ask.yml`**, same `prompt:` key under its
own "Run Claude Code" step. This one carries a real safety requirement:
- **Your prompt MUST forbid `--execute`** on every mutating subcommand
  (`set-lineup`, `add-drop`, `waiver`, `trade-propose`, `trade-respond`).
  Without that instruction, this "read-only" Q&A workflow can actually
  change your roster.
- Reference `${{ github.event.inputs.question }}` — the text you type in
  when running the workflow — somewhere in the prompt.
- Instruct writing the final answer to `./answer.md` in the repo root; the
  "Print answer" step after it just `cat`s that file.

### 7. Allow the workflows to write

Go to **Settings → Actions → General → Workflow permissions** and select
**Read and write permissions**. Each workflow declares the specific
`permissions:` it needs (`issues: write` if your prompt has the GM file
reports as issues, `contents: write` + `actions: write` for the poller), but
those only take effect if the repository-level default allows it.

### 8. Test it

Go to the **Actions** tab → **ESPN Fantasy Check** → **Run workflow** to fire
a manual run before waiting for the daily schedule. Check the resulting
GitHub issue for its report.

## Adapting it to your league

- **Acquisition type (FAAB vs. waiver priority)**: `acquisition_settings` in
  the snapshot tells you which your league uses
  (`isUsingAcquisitionBudget`) — write your prompt to branch on it so the
  same template works for either kind of league.
- **Schedule**: edit the `cron:` in `espn-fantasy-check.yml` (daily) and
  `espn-fantasy-poll.yml` (hourly) to taste.
- **How aggressive it is**: entirely up to the prompt you write for
  `espn-fantasy-check.yml` — that's where risk tolerance, trade criteria,
  and reporting format all live.

## Before you turn it loose

This is designed to run **fully autonomously and make real transactions on
your real team** — lineup changes, waiver claims, trade proposals, trade
acceptances — with no approval step, once you've written a prompt that tells
it to. Re-read your own prompt in `espn-fantasy-check.yml` with that in
mind before enabling the daily schedule, and consider running it via manual
`workflow_dispatch` for a week first to see how it behaves before letting
the cron take over.

Also note `claude-code-action` is pinned to a specific commit SHA rather
than a floating tag — the current `@v1` tag ships a version whose installer
fails silently on `ubuntu-latest` runners (see
[anthropics/claude-code-action#1817](https://github.com/anthropics/claude-code-action/issues/1817)).
Check whether that's been fixed before bumping the pin.
