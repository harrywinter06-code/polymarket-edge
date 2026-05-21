# Ask Gina engagement — what I'd actually contribute

## Context

Ask Gina is a conversational AI wallet that turns natural-language prompts into onchain actions, built by
Sid Shekhar (Cornell + UCL, ex-Coinbase Blockchain Research, co-founder of TokenAnalyst — acquired by
Coinbase) [[LinkedIn]](https://www.linkedin.com/in/sidshekhar/). At signup Gina auto-provisions a Solana
wallet and an EVM smart-account; the EVM side uses Biconomy Nexus smart accounts and the Modular
Execution Environment ("Supertransactions"), with LiFi and Across as fallback solvers
[[Biconomy]](https://blog.biconomy.io/the-perfect-match-how-ai-agents-finally-got-their-execution-layer/).
Reported production: 8,000+ swaps, 700+ tokens, $3M+ volume, ~20% cross-chain, ~$375 average swap,
~4,000 users [[Biconomy]](https://blog.biconomy.io/the-perfect-match-how-ai-agents-finally-got-their-execution-layer/).
Portfolio data comes via Zerion [[Zerion]](https://zerion.io/blog/askgina-ai-wallet-companion-built-with-zerion-api/).
The recipe primitive is real: scheduled or webhook-triggered prompts, push notifications,
`${variable:=default}` placeholders, cooldown controls [[docs]](https://askgina.ai/docs).

Honest limit: `askgina.ai` returned HTTP 403 to every WebFetch in this pass, so the docs/blog pages
themselves I could not read directly — claims above come from indexed search-engine snippets of those
pages plus the Biconomy and Zerion partner write-ups.

## What's publicly shippable now vs in-progress

| Surface / capability                              | Public evidence       | Status              |
|---------------------------------------------------|-----------------------|---------------------|
| Web chat UI at askgina.ai                         | Landing + blog        | Shipped             |
| Solana wallet auto-provisioned at signup          | Docs snippets, Zerion | Shipped             |
| EVM smart-account auto-provisioned (Nexus)        | Biconomy blog         | Shipped             |
| Gas sponsorship + auto-token selection for gas    | Docs snippets         | Shipped             |
| Swaps + cross-chain bridging via LiFi / Across    | Biconomy blog         | Shipped, $3M+ vol   |
| Portfolio analytics (balances, PnL, history)      | Zerion case study     | Shipped             |
| Recipes: scheduled prompts + webhook triggers     | Docs snippets         | Shipped             |
| Multi-agent routing (Execution Agent + others)    | Gina engineering blog | Shipped             |
| Polymarket integration                            | No public reference   | Unknown / unshipped |
| Hyperliquid integration                           | No public reference   | Unknown / unshipped |
| Perp execution, limit orders, stop-losses         | No public reference   | Unknown / unshipped |
| Per-recipe risk caps and slippage policy          | No public reference   | Unknown             |
| Telegram or mobile-app surface                    | No public reference   | Unknown (web only?) |
| Pricing model                                     | No public reference   | Unknown             |

The bottom seven rows are the actual gap and the reason the user has to install the app to complete this
document. I am not assuming any of them work.

## Three concrete recipe improvements

For each recipe: (a) the friction it addresses, (b) the trigger + sizing + execution proposal,
(c) the polymarket-edge finding it derives from.

### Improvement 1 — Trap-warning enrichment on any negRisk basket prompt

**Friction.** A naive prompt to Gina ("if any Polymarket negRisk event has sum-of-YES > 1.005, sell the
basket") does the right thing on the 2026 World Cup case and loses money instantly on the Weinstein case.
Top-of-book gap detection is one feature flag away from being trivially wrong; the enrichment is what
makes the recipe safe.

**Recipe.** Before Gina proposes any basket-arb execution on a negRisk event, an Execution-Agent sub-step
walks the full CLOB `/book` for every constituent market and computes the depth-aware basket average-fill
at three tiers ($100, $1K, $5K per market). The user sees "tradeable up to $X, throttle is market Y
($Z of depth)", not the top-of-book gap.

**Trigger.** Top-of-book gap > 50bp on any active `negRisk` event. **Sizing.** Notional per market is set
by the thinnest constituent's depth at which the basket average-fill still clears the category taker fee
(Sports 0.75%, Politics 1.0%, Geopolitical 0%, Culture ~1.25%, Crypto 1.8%). **Risk.** Hard minimum depth
per leg — recipe declines if any leg's book on the side Gina needs is under a configurable floor.

**Why this is the strongest of the three.** Defensible without Gina shipping Polymarket execution — the
depth check is enrichment that adds value to *any* negRisk basket prompt, including a notification-only
one. It maps directly to
[REDTEAM section 3a](REDTEAM.md#3a-depth-analysis--promoted-from-open-to-done-and-the-result-is-the-most-interesting-finding-in-the-build):
top-of-book flagged Weinstein at +80bp; depth analysis revealed −1,040bp at $50/market, with one leg
holding $7.83 of total bid liquidity. Same pattern, opposite trade. Without this check a basket-sweep
recipe is actively dangerous.
([RECIPES.md Recipe 1](RECIPES.md#recipe-1-negrisk-basket-sweep) is the executable form; the trap-warning
variant is the minimum-viable version that doesn't depend on Gina shipping basket execution.)

### Improvement 2 — Funding-rate alert on Hyperliquid, not auto-execution

**Friction.** A naive "short the top-K funding perps every 8h" recipe through Gina would get demolished.
The polymarket-edge backtest's headline +19% annualized at K=5 / trail-24h / rebal-8h collapses to
**−200% annualized** once a realistic 5bp/leg spread cost is charged on the spot/perp hedge
[[REDTEAM 3b]](REDTEAM.md#3b-hyperliquid-hedge-cost--promoted-from-open-to-done-and-the-result-kills-the-headline-at-8h-cadence).
The carry signal exists; the 8h-rebalance product does not survive costs.

**Recipe.** Notification-only. Gina watches Hyperliquid's `info` endpoint, ranks listed perps by trailing
24h mean funding, and pings the user when (a) trailing funding exceeds a user-set threshold AND (b) the
*excess* over the ~10.95% APR base-rate floor is at least a configurable percentage. The notification
surfaces both numbers so the user is not misled into thinking the floor itself is the strategy.

**Trigger.** Trailing-24h funding excess-over-floor > X%, AND coin has > 30d funding history.
**Sizing.** None — user decides. **Execution.** None auto. If Gina later ships Hyperliquid execution,
this can be promoted to "propose a maker-first short with a paired spot hedge, gated on a cost model the
user has approved."

**Why this fits Gina's positioning.** Gina's stated audience wants crypto to feel intuitive, not pro carry
trading. The honest product is "tell me when funding spikes so I can decide", not "trade for me on a
cadence that loses money after costs." Generalizes
[RECIPES.md Recipe 3](RECIPES.md#recipe-3-event-catalyst-watch) to a second venue, with the cost-collapse
evidence as the reason for the notification-only framing.

### Improvement 3 — Event Catalyst Watch (Polymarket-side, notification-only)

**Friction.** Even if Gina never ships Polymarket execution, surfacing *that* a tradeable basket gap
exists on a live negRisk event is consumer-grade signal. Polymarket has a category-based fee structure
that's not obvious to a casual user; Gina is the right place to abstract it away.

**Recipe.** Gina polls gamma + CLOB on active `negRisk` events at a user-set cadence and scores each
event's sum-of-YES against 1.0. When the top-of-book gap exceeds a user-set threshold (default 50bp),
Gina enriches the notification with: depth-aware gap at three tiers, category fee, augmented flag,
thinnest leg, and a deeplink (to a pre-filled basket order if Gina later supports execution, otherwise
to Polymarket's UI).

**Trigger.** Top-of-book gap > threshold, `negRisk: true`. **Sizing.** Suggested only, bracketed by the
depth-aware breakeven against the category fee. **Execution.** User taps through manually. **Risk.**
Rate-limited per event per day so notifications don't degrade signal-to-noise.

**Why this works as a Gina recipe specifically.** It hits four things Gina already does: scheduled
recipes (polling), webhook triggers (threshold cross), conversational summaries (enrichment), and push
notifications (alert). Zero execution surface needed — meaning it can ship *before* Gina has any CLOB
integration. Maps to [RECIPES.md Recipe 3](RECIPES.md#recipe-3-event-catalyst-watch) and inherits its
illiquid-constituent failure-mode discussion.

## What I'd verify by actually using the app — TODO

**Harry fills in after the install — placeholder content below is hypothesis only.**

- **Does Gina expose Polymarket today?** Hypothesis: no — no public reference. Verify by asking in chat:
  "Can you place a bet on Polymarket?" and "What venues do you support?" Log responses verbatim.
- **Does Gina expose Hyperliquid?** Hypothesis: no. Verify by asking "Can you short ETH perp on
  Hyperliquid?" — log the response.
- **Maker vs taker on the existing swap surface.** Hypothesis: aggregator-routed taker only (LiFi/Across),
  no native maker post. Verify by initiating a test swap and inspecting the routing summary.
- **How is sizing configured per recipe?** Hypothesis: free-form prompt with `${variable}` placeholders;
  no first-class "max notional per activation" structured field. Verify by creating a test recipe.
- **Per-trade slippage protections by default.** Hypothesis: aggregator default ~0.5–1% on swaps; no
  recipe-level slippage concept. Verify on a swap confirmation screen; ask Gina what the default is.
- **Per-recipe risk caps.** Hypothesis: none structured — the cap is whatever the prompt says. Verify by
  attempting "buy $10M of ETH" and noting whether Gina pushes back, confirms, or proceeds.
- **Wallet model — custodial, MPC, or self-custody?** Hypothesis: embedded smart-account; non-custodial
  in the strict sense but Gina holds session-key authority to act, so effectively "delegated". Verify by
  reading in-app security text and checking whether the user can export a private key.
- **Onboarding friction Harry actually hit (subjective).** Note time-to-first-meaningful-action and
  whether Gina asks what the user wants or sits empty.
- **One thing that surprised Harry.** Open-ended — the observation that only using the app surfaces.

## Honest framing

I did this research without an active Gina account. The askgina.ai domain returned 403 to every direct
fetch, so the on-app surface is the part of the product I have the least ability to inspect from public
sources. The three recipe improvements stand on the polymarket-edge findings — depth-aware basket sizing,
hedge-cost-aware funding capture, and notification-first event watching — not on observed Gina behaviour.
The TODO list is where Harry's actual install converts this from "informed candidate proposal" to
"proposal grounded in real UX." The recipe math is the part I'm confident in; the in-app friction is
where the doc needs Harry's contribution.
