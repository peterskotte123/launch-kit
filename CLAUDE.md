# launch-kit - notes for Claude

See README.md for layout and API. PermitGap (`~/Projects/PermitGap`) is the first app on the kit, and its
`scripts/stripe_roundtrip.py` is the end-to-end check (needs the app on :8000 plus `stripe listen`).

## Learnings

### Problem
`pkill -f "uvicorn app.main:app --port 8000"` (and `pkill -f "stripe listen ..."`) killed the shell running it (exit 144).

### Root Cause
`pkill -f` matches full command lines, and the bash command running pkill contains the same pattern.

### Solution
Stop servers by port (`fuser -k 8000/tcp`) or by exact process name (`pgrep -x stripe`).

### Key Learning
Never `pkill -f` with a pattern that also appears in the command doing the killing.

### Problem
Headless Stripe Checkout tests timed out waiting for `input#payment-method-accordion-item-title-card` (Sept 2026).

### Root Cause
Stripe changed the hosted Checkout markup. Card is now `button[data-testid=card-accordion-item-button]`. That button is
hidden and covered by another element, so neither a normal click nor a visibility wait works.

### Solution
`wait_for_selector(..., state="attached")`, then `locator(...).dispatch_event("click")`.

### Key Learning
Selectors on Stripe's hosted pages drift. When a checkout test fails before payment, screenshot the page first. The
cause is usually the page markup, not the app.
