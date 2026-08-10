# Admin: Managing Modules & Feature Flags

Read [../features/feature-flags.md](../features/feature-flags.md) first for the concept — this page is the step-by-step "how to actually do it" guide.

## Toggling a Whole Module On/Off

1. Go to **Admin → Modules**.
2. You'll see every module (Home, Events, Hackathons, CodeFest, Codequest, Career, Membership, Resources, Community, Blog) with a switch.
3. Flip a module **off** → it's removed from the public sidebar and becomes unreachable for everyone except Admins (who still see it, marked "disabled," inside the Admin Panel).
4. Flip it back **on** → it reappears immediately. No deployment, no waiting.

**When to use this:** pausing a module for a season (e.g. Hackathons in the off-season), soft-launching a module before announcing it publicly, or temporarily disabling something under maintenance.

## Setting a Date-Based Visibility Window (Per Item)

This is different from the module switch above — it controls **one specific item** (one event, one hackathon, one blog post, one Career listing), not the whole module.

1. Open that item's edit screen (e.g. **Admin → Events → [event] → Edit**).
2. Find the **Visibility** section.
3. Set `Visible From` and `Visible Until` (dates and, where relevant, times).
4. Save. The system will automatically show/hide this specific item exactly on those dates — you don't need to come back and do anything manually.
5. Optional: check **"Always visible"** to override auto-hiding (useful for permanent pages like results archives).

**When to use this:** any time-bound content — a workshop that shouldn't show after it happens, a hackathon whose registration should close on a deadline, a Career listing that should vanish after its application deadline.

## Quick Reference: Which One Do I Need?

| I want to... | Use |
|---|---|
| Hide an entire module (e.g. "no Hackathons this semester") | Module toggle |
| Hide one specific event/hackathon/listing after its date passes | Per-item visibility window |
| Keep a module visible but only show one item early/temporarily | Per-item visibility window, with the module toggle left **on** |

## Related Docs
- [../features/feature-flags.md](../features/feature-flags.md) — how this works conceptually
- [../features/scheduling.md](../features/scheduling.md) — the background engine that checks dates
- Every module doc's "Visibility Rules" section (e.g. [../modules/hackathon.md](../modules/hackathon.md#visibility-rules-feature-flag--event-dates))
