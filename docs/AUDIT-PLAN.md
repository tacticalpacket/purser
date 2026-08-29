# Money Leak Audit and Tracking Plan

Working document. Researched August 27, 2026.

## The short version

1. There is no tool, paid or free, that will pull two years of history for you. Bank-sync services (SimpleFIN, Plaid, MX) backfill about 90 days, sometimes less. Anything older has to come from an export you download yourself.
2. That export is far less painful than you think: one CSV per account covering the whole date range, not one PDF per month. Navy Federal gives up to 18 months per pull; Schwab gives several years. Expect 4 to 8 files total, roughly 20 minutes of clicking.
3. Once you have the CSVs, a full cash-flow audit (every dollar categorized, monthly view by category, weekly habit spend, recurring charges) is an afternoon with Claude Code, not a project. That is what shuts the valve this week.
4. For ongoing tracking, the best fit for you is Actual Budget (open source, local-first, runs in Docker on Tombstone) plus SimpleFIN Bridge ($15/yr) for automatic sync going forward. It has built-in recurring-charge detection and there are mature MCP servers so you can just ask Claude questions about your money.
5. If you want zero setup and are willing to pay and to let a third party hold read-only access, Monarch Money ($99/yr) is what most people use. It is the fallback, not the recommendation.

---

## Phase 1: The two-year audit (this week, one time)

Goal: every dollar in and out over the window, sorted and accounted for, so the whole picture is visible at once. Subscriptions are one slice. The output has four views:

1. Full ledger: every transaction, normalized merchant, category, account. Nothing uncategorized when done.
2. Cash flow by month: income vs. outflow, and outflow broken into fixed (rent, insurance, loans, utilities), recurring discretionary (subscriptions, memberships), and variable (food, fuel, shopping, dining, everything else). This is the "where is it all going" view.
3. Habit spend: merchants you hit frequently for small amounts. Weekly coffee, delivery apps, convenience stores, fuel, fast food. These do not look like subscriptions but behave like them. Ranked by monthly total and by visit count.
4. Recurring charges: the subscription table described below, ranked by annualized cost, with keep / kill / verify verdicts.

Anything that is a leak shows up in view 2 as a category that is too fat, in view 3 as a merchant that is too frequent, or in view 4 as a charge you forgot. All three get a verdict, not just the subscriptions.

### Step 1. Export

Navy Federal (as of April 2026 they dropped QFX/OFX/QIF; Digital Banking offers CSV and PDF only)
- Digital Banking > select account > Transaction History > Download (far right) > CSV
- Set the custom date range to the max. Online history reaches about 18 months.
- Do this once per account: checking, savings, each credit card. Anything older than 18 months requires PDF statements, which I would skip for now; 18 months is enough to catch every annual charge at least once.

Schwab
- Accounts > History > select account > custom date range > Export > CSV
- History goes back several years online; the export caps at 10,000 rows, so split the range if needed.
- Do this for Investor Checking and any brokerage account you pay things from.

Naming convention: `NFCU_checking_2025-03-01_to_2026-08-27.csv`, `Schwab_checking_2024-08-27_to_2026-08-27.csv`, and so on. Drop them in one folder.

### Step 2. Normalize, categorize, and analyze

Hand the folder to Claude Code (or upload to this chat). The job is a short Python/pandas script plus LLM labeling:

Ledger build
- Read every CSV, map each bank's column names to date / description / amount / account.
- Normalize merchant descriptions (strip store numbers, dates, trailing IDs, "POS", "ACH", city codes).
- Deduplicate transfers between your own accounts (NFCU to Schwab, card payments from checking) so money moving between pockets is not counted as spending.
- Assign every normalized merchant a category. The script handles the obvious ones by keyword; the LLM handles the rest in one pass over the unique-merchant list (usually a few hundred names, not thousands of rows). Categories: housing, utilities, insurance, loans/debt, groceries, dining, delivery/takeout, coffee/convenience, fuel, auto, medical, subscriptions/SaaS, memberships/gym, shopping, home/tools, travel, transfers, income, unknown. You will adjust; this is a starting taxonomy.

Cash flow by month
- Pivot: month x category, plus income total and net.
- Each category gets a 12-month average and a trend so the fat ones and the growing ones stand out.

Habit spend
- Group by normalized merchant; compute visit count, total, average ticket, visits per week.
- Rank by monthly total. Anything with 4+ visits a month and a small ticket is a habit line, and gets the same keep / cut / reduce verdict as a subscription.

Recurring charges
- For each merchant compute median interval between charges, amount variance, first seen, last seen.
- Flag as recurring if interval is stable (weekly, monthly, quarterly, annual within a tolerance) and amount is stable.
- Annualize: weekly x52, monthly x12, quarterly x4, annual x1.
- Output sorted by annualized cost.

Verdicts are judgment, so the script does the math and the LLM does the labeling. Every merchant and every category gets one:
- KILL: recurring or habitual, no plausible current use, or duplicate of another service
- CUT: real spend but the category or merchant is far above what the situation supports; set a cap
- VERIFY: might be worth it, you decide
- KEEP: rent/insurance/utilities/essential
- UNKNOWN: merchant not identifiable, look it up

Privacy note: if you do not want the raw CSVs leaving the house, the same script plus the DeepSeek instance on Cortex can do the labeling fully local. Claude Code is faster and better at merchant identification; your call.

### Step 3. Kill list and caps

Two outputs from the tables: a cancellation list (recurring charges to kill) and a cap list (categories and habit merchants with a monthly ceiling). The cap list is what actually moves the needle on variable spend; cancellations are the easy wins. Practical notes:
- Gyms often require in-person or certified-mail cancellation and bill through a third party (ABC Fitness, Paramount Acceptance). The merchant name on the statement may not be the gym's name.
- Annual SaaS and domain/hosting renewals are the ones that "slide in". Look for anything with count = 1 or 2 and interval near 365 days.
- For every VERIFY item, the question is "did I use this in the last 60 days." If no, kill it. You can always resubscribe.
- Card-on-file subscriptions: cancelling at the merchant is the reliable path. Cancelling the card only works for some and generates collections risk for others (gyms especially).

Expected output of Phase 1: the normalized ledger (CSV), the month x category cash-flow table, the habit-spend ranking, the recurring-charge ranking, the kill list, and the cap list. All saved alongside this document. The ledger is also what gets imported into Actual Budget in Phase 2, so the categorization work is done once.

---

## Phase 2: Ongoing system (so this does not sprawl again)

### Recommended: Actual Budget + SimpleFIN Bridge, self-hosted on Tombstone

Why this one
- Open source (MIT), local-first, optional end-to-end encryption, runs from a single Docker container. Fits your existing Tombstone stack.
- CSV/OFX/QFX import for the Phase 1 history, so the audit data becomes the starting ledger.
- Built-in "Find schedules" button scans transaction history and proposes recurring payments. That is the subscription detector, and it is native.
- Rules engine for auto-categorization and payee normalization.
- Bank sync via SimpleFIN Bridge: $1.50/mo or $15/yr, up to 25 institutions. SimpleFIN runs on MX for the bank connection, emails you whenever a new IP touches your token, and you can revoke access in one click. It is the one third party in the chain, and it is a small company whose stated mission is to make itself unnecessary.
- Multiple community MCP servers (agigante80/actual-mcp-server with ~70 tools, s-stefanov/actual-mcp, henfrydls/actual-budget-mcp) connect Claude Desktop or Claude Code directly to your instance. "What did I spend on subscriptions last quarter" becomes a chat question.

Setup order
1. Docker compose for actualbudget/actual-server on Tombstone, behind whatever you already use for TLS.
2. Create the budget file, enable E2E encryption.
3. Import Phase 1 CSVs, one account at a time.
4. Run Find schedules, accept the real ones, delete the noise.
5. Subscribe to SimpleFIN Bridge, link NFCU and Schwab, generate a setup token, paste into Actual. Verify both institutions are listed at beta-bridge.simplefin.org/search-institutions before paying; both are large and NFCU still supports aggregator connections even after dropping Quicken file downloads, but confirm.
6. Add an Actual MCP server to Claude Code so future audits are a question, not a script.

Caveats
- SimpleFIN backfills 90 days max. Phase 1 exports are still required.
- Bank sync connections break periodically at every provider. Expect to re-authenticate a few times a year.
- Actual is a budgeting tool at heart (envelope method). You do not have to budget; you can use it purely as a ledger and reporting tool. Ignore the budget tab if you want.

### Alternatives, kept on the table

Paid, polished, zero setup (what most people use)
- Monarch Money, $99.99/yr. Best all-around dashboard, Android and iOS, household sharing, does not sell data, SOC2. Requires bank linking through Plaid/MX; no manual-only path. Sends de-identified data to outside AI models under no-train terms.
- Copilot Money. Best design, strong recurring detection, does not sell data. Apple platforms only (iPhone, iPad, Mac, web beta). No Android.
- Rocket Money (formerly Truebill). Built specifically around finding and cancelling subscriptions, has a usable free tier. Owned by Rocket Companies (the mortgage lender). Variable premium pricing $6 to $12/mo, bill-negotiation takes a percentage of savings. Says it does not sell data. This is the one I would trust least with read access, purely on business-model grounds.
- YNAB, roughly $109/yr. Budgeting methodology first, subscription tracking second. Overkill for your stated goal.

Open source, self-hosted, other options
- Firefly III (AGPL, PHP). Heavier, double-entry accounting, has a "subscriptions" feature and rules. More capable and more work than Actual. Choose it only if you want proper accounting.
- Sure (community fork of the abandoned Maybe Finance, Ruby). Prettier, less mature, bank sync still settling.
- Wallos (GPL, PHP). Not a ledger; a manual subscription registry with renewal reminders. Useful as the "here is everything I intentionally pay for" list after the audit. Could sit next to Actual.
- GnuCash / HomeBank. Desktop, manual import, no sync. Solid, dated.

No-bank-link trackers (ReSubs, SubBuddy, SenticMoney, Bobby)
- Manual entry only. They cannot find what you forgot about, which is the whole problem. Skip.

### What creative people do with Claude on top of this

- Actual MCP + Claude Code: monthly "leak report" run as a scheduled session. Query all transactions for the month, diff recurring merchants against the approved list in Wallos or a YAML file, flag new or changed recurring charges.
- Local-only variant: same query layer, but classification runs on Cortex's DeepSeek so nothing leaves the LAN.
- Annual-charge early warning: from the schedules table, anything with a 12-month cadence gets a reminder 30 days before the next hit so you can cancel before renewal rather than after.

---

## Decision log

- 2026-08-27: Researched. Recommendation is Phase 1 audit via CSV export + Claude Code script now; Phase 2 Actual Budget + SimpleFIN on Tombstone for ongoing. Monarch as the paid fallback if self-hosting is not worth the time right now.
- 2026-08-28: Direction changed to building a custom platform (repo: purser) from a GPT-drafted spec, reviewed and scoped in purser-repo-design.md. Actual Budget demoted to fallback if the build stalls. Phase 1 export steps unchanged and now feed purser M1.

## Sources consulted

- Actual Budget docs: schedules and Find schedules, SimpleFIN setup (actualbudget.org/docs)
- SimpleFIN Bridge (beta-bridge.simplefin.org), Skwad docs on SimpleFIN 90-day backfill limit
- Quicken community thread, April 2026: NFCU email confirming CSV and PDF only
- DocuClipper / BankXLSX guides on NFCU 18-month history and Schwab export
- LinuxLinks self-hosted finance roundup (June 2026), opensourcetools.org personal finance category
- Monarch, Copilot, Rocket Money 2026 comparisons (envelopebudgeting.com, budget-calm.com, getfinny.app)
- Actual MCP servers: agigante80/actual-mcp-server, s-stefanov/actual-mcp, henfrydls/actual-budget-mcp
