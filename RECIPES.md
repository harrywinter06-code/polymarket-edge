# RECIPES.md — from research to Gina recipes

## 1. Framing

Ask Gina is an AI crypto wallet whose product surface is *recipes*: user-approved financial automations that the wallet can execute across Polymarket, Hyperliquid, and adjacent venues. A recipe is a packaged decision — trigger condition, sizing logic, execution path, risk caps — that a user opts into once and the wallet runs on their behalf.

The polymarket-edge prototype is a generic event-level no-arb scanner plus a Hyperliquid funding-capture backtest, with a depth-aware basket-fill module bolted on after the red-team pass. The findings translate directly into Gina recipes because each finding answers a recipe-shaped question: *when does the wallet act, how much does it size, what kills the trade*. The three recipes below take the project's strongest empirical results and the most expensive lesson (Weinstein) and turn them into shippable automations at three different levels of user-facing automation.

## 2. Three concrete recipes

### Recipe 1: NegRisk Basket Sweep

**What it does.** When a Polymarket `negRisk` event has a sum-of-YES gap that clears its category fee at executable size, Gina sells (or buys) the full basket of YES tokens across all constituent markets to capture the convergence to $1.

**Trigger condition.** Top-of-book gap exceeds 50bp on a `negRisk` event AND the depth-aware basket fill — walking the full `/book` on every constituent market — still clears the category taker fee (Sports 0.75%, Politics 1.0%, Geopolitical 0%). The World Cup result is the reference case: 150bp top-of-book, 150bp at $1K per market, ~75bp net after the Sports taker fee.

**Sizing logic.** Notional per market is set by the *thinnest* constituent's bid (or ask) depth at which the basket-average fill still clears fees. The basket is throttled by the weakest leg — on the World Cup signal, Iran's bid book exhausts around $3K and caps the basket at ~$145K total, with the comfortable size at ~$48K ($1K × 48). Gina computes this per signal before proposing the trade; the user sees a single notional number with a one-line explanation of which market is the bottleneck.

**Execution.** Maker-first: post limit orders at the inside on every leg to earn the 20–25% maker rebate (this clears every gap the project surfaced). If a leg isn't filled within a configurable window, Gina escalates the unfilled legs to taker. Settlement uses Polymarket's negRisk converter contract so the user holds one $1-equivalent basket rather than N redundant collateral positions — material capital efficiency on 48-market events.

**Risk caps.** Hard max notional per user per recipe activation; hard max per event (no doubling down on a re-flag); per-event circuit breaker that disables the recipe on `negRiskAugmented: true` events once executable size exceeds a low cap, because the sum=1 bound is structurally softer when new outcomes can be added mid-event. The Weinstein lesson hard-codes a *minimum depth per leg*: any constituent market with under $X of book on the side Gina needs to hit kills the trade. $7.83 of depth on one leg is what turned an 80bp top-of-book signal into a 1,040bp loss.

**Honest failure mode.** Illiquid constituent markets, full stop. The project's depth analysis shows the same top-of-book gap can be a clean 150bp on a 48-market sports event and a 10,000bp loss on a 6-market sentencing event. Without the depth check this recipe is actively dangerous; with it, the recipe is conservative-by-construction.

### Recipe 2: Top-K Funding Capture

**What it does.** Gina shorts the top-K highest trailing-funding perps on Hyperliquid, equal-weighted, rebalanced every 8 hours, and collects the funding flow paid by longs.

**Trigger condition.** Always-on while the user has the recipe active and sufficient margin. At each 8-hour funding tick, rank the listed perps by trailing 24-hour mean funding rate and select the top K (the prototype tested K=3, 5, 10).

**Sizing logic.** Equal notional per coin within the basket. Total basket notional is bounded by the user's available collateral and a margin buffer that keeps the position above the maintenance threshold under a stress move. The project tested 30 days and ~56 rebalances; the trailing-24h predictor captured ~85% of the perfect-hindsight ceiling, so the recipe doesn't need to be clever about selection.

**Execution.** Maker post-only into the Hyperliquid orderbook at each rebalance, with fallback to taker after a short timeout — the rebalance cost matters because there are ~1,095 rebalances per year and slippage compounds. The K=5 variant is the default because it scored Sharpe 37 (funding-only, upper bound) and 98% hit rate, materially better tradeoff than K=3 on noise grounds.

**Risk caps.** Per-user max basket notional. Hard exclusion on perps with less than N days of funding history (no new-listing pile-ins). Per-coin cap as a fraction of basket so a single memecoin can't dominate (FARTCOIN was the highest single-coin yield in the data at +17.7% annualized but tiny markets are de facto position-limited). Kill switch if realized 8h funding drops below a floor that suggests the regime has shifted.

**Honest failure mode.** The +19% top-5 annualized result decomposes into roughly +11pp from the Hyperliquid base-rate funding floor (~10.95% APR — passive carry any coin near zero premium pays) and only +8pp of genuine excess from coin selection. The recipe's real edge is that 8pp, and the *unmodeled* hedge-leg cost (spot/perp basis, spot funding, slippage, liquidation buffer drag) eats into it before the user sees net P&L. Without a spot hedge integration this recipe is directional short crypto, which is not what it advertises.

### Recipe 3: Event Catalyst Watch

**What it does.** A notification-only recipe — no auto-execution. Gina watches all live `negRisk` Polymarket events and pings the user when sum-of-YES drift exceeds a fee buffer, attaching the depth-aware basket gap so the user can decide whether to execute manually through the wallet.

**Trigger condition.** Top-of-book gap exceeds a user-set threshold (default 50bp) on any active `negRisk` event. Gina enriches the notification with the depth-aware gap at three notional tiers ($100, $1K, $5K per market), the category fee, the augmented/non-augmented flag, and the thinnest constituent.

**Sizing logic.** None — the user sizes manually. Gina suggests a notional bracketed by the depth-aware breakeven against the category fee.

**Execution.** User taps through to a pre-filled basket order in the wallet UI. Maker-first by default with a one-tap escalate-to-taker.

**Risk caps.** No automated capital at risk. The cap is on notification frequency (rate-limit per event, per day) so the user isn't spammed and so the recipe doesn't degrade the wallet's signal-to-noise.

**Honest failure mode.** Same as Recipe 1 — illiquid constituent markets generate top-of-book signals that look real and aren't. Gina mitigates by leading the notification with the depth-aware number, not the top-of-book gap, so the Weinstein-class trap is visible before the user taps. This is the lowest-risk way to introduce the negRisk pattern to a new Gina user; it's also the broadest applicability because most flagged signals aren't worth executing at any size, and a notification recipe still adds value there.

## 3. What Gina would need to build that polymarket-edge hasn't

The prototype is a research stack, not a product. To ship the recipes above, Gina would need:

- **Spot leg execution for funding capture.** The Hyperliquid backtest measures funding flow only; the recipe needs a paired spot long (or alternative hedge) and a real basis/slippage model. Without it, Recipe 2 is short-the-market dressed up as carry.
- **A streaming order-book feed.** The current depth detector polls top-of-book and walks `/book` on demand. Production needs an event-driven feed so basket sweeps trigger and size in seconds, not poll intervals.
- **Per-user position and risk state across active recipes.** A user running Recipes 1, 2, and 3 simultaneously needs aggregated margin and exposure, not three independent risk silos.
- **Withdrawal and settlement flow integrated with the wallet UX.** NegRisk converter redemption, USDC accounting, tax-event surfacing — none of this exists in the prototype.
- **Compliance and jurisdiction gating.** Polymarket access varies by country and recipes must be gated accordingly; the prototype is jurisdiction-blind.
