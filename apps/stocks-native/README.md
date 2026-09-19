# StocksNative 0.2.0 validation track

An iPad-first native SwiftUI client with live market summaries, intraday and
multi-period Swift Charts, stock analytics, Apple sign-in, local caching and an
AVAudioEngine voice sidebar. The app embeds no WebView, HTML chart or website UI.
The Reader project is a read-only reference.

The independent bundle is `space.bwicarus.stocksnative`; it can coexist with
BWReader. Existing Apple distribution signing material is reused through GitHub
secrets. StocksNative needs its own provisioning profile and App Store Connect
record. Building an IPA does not mean TestFlight or a physical device was tested.

## Scope

This version validates live stock data, native display, bidirectional voice,
screen-context injection and a structured native chart annotation layer. It
includes market breadth, quotes and five-level order books, intraday data,
5/15/30/60-minute and daily/weekly/monthly K-lines, technical indicators, capital
flow, chips, concepts, peers and announcements. No trading operations are exposed.

The native left sidebar separates market search, observation groups and the
screener. Screening restores the original 25 conditions with OR between groups,
AND/NOT within each group, numeric thresholds, per-condition switches and removal
impact counts. Missing data remains unknown and cannot satisfy a NOT rule. Saved
schemes, manual groups and smart groups share one account-scoped service with AI.
Smart groups support hot-sector membership and filter definitions; holding,
historical holding and old AI-rating attributes explicitly require migration.
They are not silently ignored and do not trigger background AI calls.

## Gateway

Base URL: `https://bwicarus.space/stocks-native`.

- `POST /api/auth/apple`: verifies an Apple identity token and hashed nonce, then
  issues a device-bound token. One-use pairing remains available only in the
  folded review/development entry.
- `GET /api/market/overview`: market breadth and hot sectors.
- `GET /api/realtime?codes=...`: live quotes and five-level order books.
- `GET /api/stocks`: stock search; `GET /api/stocks/{code}`: quote and analytics.
- `GET /api/stocks/{code}/intraday` and `/kline?period=...`: native chart data.
- `GET /api/selection/catalog` and `/library`: conditions, thresholds and the
  authenticated account's saved schemes/groups.
- `POST /api/selection/evaluate`: snapshot screening, paging and condition impacts.
- `POST /api/selection/mutate`: explicit group/scheme operations, requiring
  `requestId` and `expectedRevision`. Retries reuse the same request; stale edits
  return HTTP 409 instead of overwriting another device or AI change.
- `GET /voice?deviceId=...`: WebSocket carrying JSON events and 20 ms PCM16LE mono
  48 kHz audio frames. The bearer token is passed only in an authorization header.
- `GET /api/health`: unauthenticated minimal version/liveness response.

The VPS runs a separate `stocks-native` service user and port 5012. nginx adds
only a new path and is gracefully reloaded. Existing websites and their service
processes are not restarted. The new Codex home contains its own authenticated
session and no inherited Reader tools or plugins. Each voice connection owns its
own Codex app-server process and WebRTC connection; same-device duplicate calls
are rejected rather than replacing an existing call.

`refresh_market.py` reads the production database using SQLite online backup into
new generation directories. It preserves original market timestamps and skips
unchanged sources. Publishing switches a symlink atomically; failures retain the
previous copy. The gateway itself only reads this copy.

Full finalized transcripts and tool receipts are persisted per device, and the
Codex thread ID is resumed. Realtime startup currently loads only eight recent
transcripts, with bounded text. This is durable storage, not a guarantee that
every historical detail is automatically present in the model context.
The deployed service's `STOCKS_VOICE_IDLE_SECONDS` setting bounds idle calls
(currently 20 minutes); closing the App socket tears down its voice process.

The current 0.2.0 build track advertises `chart.annotation.v1` and `ui.context.v1`.
App updates are merged for 350 ms and only replace the VPS's cached snapshot.
They do not independently trigger model input. User speech pins the current UI
target; delegation uses that same target. Context is split into independently
acknowledged sections: voice receives the stock, actual tab/sidebar, quote,
selected chart point and last key action; backend receives bounded visible-panel
metrics, chart range, book and structured annotations. Actions expire after 30
seconds and are scoped to the current stock/period. `visibilityScope=active_tab`
does not imply per-card scroll visibility. Freehand stroke counts do not provide
visual understanding of the drawing.

Only changed sections are sent. Within the same stock/day/source, quote changes
below 0.05 percent in price and 0.05 percentage points in changePct are held against
the last successfully injected quote for at most 60 seconds (only evaluated when
the user speaks). Crossing zero, changing stocks or an explicit live-data question
bypasses the relevant filter. `STOCKS_CONTEXT_PRICE_PERCENT` and
`STOCKS_CONTEXT_CHANGE_POINTS` configure these thresholds. Explicit live questions
refresh only the requested quote/book/metric fragment through the shared 5-second
quote cache; provider market time is preserved and unavailable refreshes are
marked. This is not a guarantee of exchange-tick latency or realtime voice timing.

Full technical/fund/chip history, announcements, peers and historical charts are
tool-only. New threads expose `stocks_context` with selected sections; resumed
threads keep their original tools/history and their current/detail queries use
the same targeted reader when the question identifies a component. No extra model
classifies each UI event. UI actions retain request/receipt verification before
the assistant can claim a mark was applied.

Account selection operations use a session-scoped `stocks_selection` stdio MCP
configured at both thread start and resume. The authenticated owner is fixed by
the gateway, never supplied by the model. Existing threads retain their ID and
history. Successful mutation receipts refresh the native library. Install
`requirements-selection.txt` together with `requirements.txt` in a candidate
release's isolated virtual environment; `STOCKS_SELECTION_PYTHON` can override
the MCP interpreter. Never upgrade the serving environment in place.

Apple logins link devices to one account library. Pairing-only devices remain
isolated. Legacy pre-owner tokens are linked only when one exact recorded Apple
login timestamp establishes the association. Selection state lives separately in
`selection.sqlite3`; revisioned writes are atomic. A private administrative import
can append old schemes/groups and retains originals, including unsupported rules.
The import is not an HTTP or AI operation. Device-to-account adoption copies only
into an empty account library and preserves the device source.

The native workspace is a twelve-column two-dimensional canvas. Cards move from
their own grab handles, snap to adjacent card edges or the canvas boundary, and
resize from a corner or a shared divider. Divider junctions resize both axes
together, with neighboring cards sharing the new boundaries. Overlaps push neighboring cards down;
the drop preview shows the resulting placement. The bottom lock protects layout
while chart gestures remain available; the card library manages tabs and visibility.
The upper-right toggle exclusively opens or closes the AI sidebar.
Grab handles use a compact header, which disappears when the layout is locked.
Fund cards switch between stacked and side-by-side content as their aspect ratio
changes, retaining the selected date and all flow metrics.
Chart range controls stay at the bottom of each chart card while its details
scroll separately. Intraday price and volume plots use a compact height budget.
Fund charts default to the latest five trading days with amounts in 100 million
CNY; selected-day flows are separated from the dated latest summary. All eight
buy/sell tiers remain listed, including zero amounts and explicitly missing data.

Card coordinates and sizes are saved in Application Support independently of
market-cache cleanup. The earlier ordered half/full-width layout is converted
without discarding tabs or hidden cards and backed up before its first upgrade
save. The simulator build runs focused Swift checks for migration, snapping,
collision resolution and shared-divider sizing; physical touch feel still needs
device verification.

The App displays cached overview, list, detail and chart data immediately, then
refreshes from the VPS. Intraday cache lives for 12 hours, overview/list for 24
hours and detail/K-lines for 7 days. Startup cleanup removes entries older than
14 days and keeps the cache below 80 MB. Long-term datasets and computation stay
on the VPS.

Typed requests go directly to the persistent Codex thread through `turn/start`.
Voice requests use the CLI's native delegation. Both backend result paths share
one `appendSpeech` output; automatic result handoff is disabled to avoid duplicate
speech. Each result carries request/turn/tool receipts. A submitted speech request
is not evidence that the user heard it; the verification probe also checks actual
non-silent audio and matching stock, price and date in the final transcript.
The speech layer may paraphrase wording; it is not a verbatim TTS guarantee.

## Operations

Source: `/opt/stocks-native`; private state: `/var/lib/stocks-native`.
Issue a code directly on the VPS as the service user:

```sh
runuser -u stocks-native -- /opt/stocks-native/venv/bin/python \
  /opt/stocks-native/server/auth.py \
  --state-dir /var/lib/stocks-native/state create-code
```

Only the new gateway and snapshot timer need stopping to roll back this MVP.
The original nginx file is backed up under `/opt/stocks-native/backups`; when
removing the new route, preserve any subsequent unrelated nginx edits and run
`nginx -t` before a graceful reload. Never restart the old stock/Reader services
as part of a native app build.

## Build and evidence

See `scripts/BUILD.md` and `ios/StocksNative/README.md`. The independent workflow
builds a simulator target before creating a separately signed IPA. Runtime tokens,
credentials, market datasets, voice journals and probe outputs must not be
committed to the public source repository.

Voice concurrency probes use isolated processes. A generated or silent audio
fixture proves transport behavior, not an iPad microphone, speaker, Bluetooth or
echo-cancellation acceptance test.
