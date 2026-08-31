# Experimental G2 navigation field run

This is an experimental, wearer-operated acceptance procedure for one
walking and one cycling route. It captures only fixed semantic states and
location-quality buckets. It never retains or exports a route, coordinates,
place name, spoken destination, audio, device ID, or provider token.

## Evidence levels

Keep these independent:

| Evidence | Establishes | Does not establish |
| --- | --- | --- |
| Focused code tests | redaction, event validation, and field-run summary rules | phone, BLE, or lens behavior |
| Official simulator | gesture-to-screen routing and rendered frames | phone GPS, BLE latency, temple source, or lens visibility |
| Portal/phone install | the named package reached the phone | displayed pixels on G2 |
| Continuous lens video | what the wearer could see and the gesture flow | destination/provider correctness beyond the demonstrated run |

## Before either run

1. In the iPhone Settings page for the Even Hub host app, allow Location.
   “Phone location unavailable” is a phone-fix/permission result; a Mapbox
   token cannot grant it. Correct that while stationary, then retry from the
   glasses.
2. Install the exact packaged version named for this run, then reopen JOAT on
   the phone. The phone must stay foregrounded and unlocked.
3. Confirm the G2 is paired and the Mac bridge is connected.
4. Start normal JOAT with `--g2-diagnostics` only. Do **not** enable
   `--location-probe` during navigation: it is a separate high-accuracy
   session and intentionally cannot preempt a route.
5. Confirm a navigation provider is configured. The current provider is
   Mapbox. When JOAT starts from Finder, it deliberately reads only
   `MAPBOX_ACCESS_TOKEN` (or the legacy `MAPBOX_API_KEY`) from the selected
   repository's local `.env` and maps it to the engine contract. It forwards
   no other `.env` values. A configured token enables walking/cycling
   navigation; if no token is present, navigation shows the provider-unavailable
   screen instead of falling through to the general assistant.
6. Start one continuous video before speaking. Film the G2 optics closely
   enough to read each HUD transition. Mute the video before sharing if it
   includes the spoken address. Do not send an address screenshot or screen
   recording.

## Wearer flow — no sidebar or command line navigation

1. Say: `JOAT, navigate to …`.
2. On the G2 destination screen, **tap** to confirm the canonical place. That
   visible screen is the `start` lens-video marker.
3. On the G2 mode screen, **scroll** to walking or cycling and **tap**. The
   route preview must follow; tap to start.
4. Follow the on-glasses guidance. Keep the phone foregrounded. The video
   must show at least one guidance cue and every warning/recovery state that
   occurs naturally. Do not intentionally walk unsafely to provoke a warning.
5. End with the documented left-temple double press, or arrive. The visible
   stop/arrival transition is the `end` lens-video marker.
6. Stop the video only after the end transition.

Run walking and cycling separately. Stop immediately if the HUD is blank, the
phone location session is denied, no guidance cue appears, or the bridge
disconnects. Report that run as `fail` or `inconclusive`; never retry while
moving.

## Produce the redacted field-run summary

After the normal run, convert its owner-only diagnostic receipt into a
separate summary. The command reads a pre-sanitized receipt and creates a
new `0600` file; it does not open a gateway or request location.

```sh
PYTHONPATH=apps/mac .venv/bin/python scripts/real_g2_location_probe_trace.py \
  --summarize-navigation-field-run \
  --mode walking \
  --source-receipt "$HOME/Library/Application Support/JOAT/g2-diagnostics.json" \
  --out /private/tmp/joat-walking-field-run.json
```

Repeat with `--mode cycling` and a different `--out` path. A summary has:

- fixed `hudCues` with receipt timestamps;
- bucket counts for accuracy, age, cadence, displacement, heading availability,
  and foreground/background state;
- `start` and `end` marker timestamps tied to the visible destination and
  stop/arrival transitions;
- `receiptResult`, which means only that the redacted bridge receipt is
  complete; and `lensVideoResult: required`, because acknowledgements are not
  proof of visible pixels.

## What to send back

For each mode, attach the short continuous, muted lens video and the
corresponding `joat.g2-navigation-field-run.v1` summary. If a run fails before
guidance, attach the video of the error/recovery surface and say at which
numbered step it stopped. Do not attach the raw route, address, transcript,
or the raw diagnostic receipt unless you explicitly choose to share it.
