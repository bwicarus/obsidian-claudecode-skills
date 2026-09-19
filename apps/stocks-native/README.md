# StocksNative 0.2.0 validation app

An iPad-first native SwiftUI client with Swift Charts candlesticks, stock search,
stock detail and an AVAudioEngine voice sidebar. The MVP embeds no WebView,
HTML chart or website UI. The Reader project is a read-only reference.

The independent bundle is `space.bwicarus.stocksnative`; it can coexist with
BWReader. Existing Apple distribution signing material is reused through GitHub
secrets. StocksNative needs its own provisioning profile and App Store Connect
record. Building an IPA does not mean TestFlight or a physical device was tested.

## Scope

This first version validates stock data, native display and bidirectional voice.
The larger agreed feature migration, native annotations and full AI navigation
will follow after this MVP is checked on an iPad. Current market data is a dated
snapshot, not a streaming real-time feed. No trading operations are exposed.

## Gateway

Base URL: `https://bwicarus.space/stocks-native`.

- `POST /api/pair`: one-use 10 minute pairing code, device ID and device name.
- `GET /api/stocks`: stock search; `GET /api/stocks/{code}`: details and daily candles.
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
Ten minutes without conversational activity closes an idle voice session.

Typed requests go directly to the persistent Codex thread through `turn/start`.
Voice requests use the CLI's native delegation. Both backend result paths share
one `appendSpeech` output; automatic result handoff is disabled to avoid duplicate
speech. Each result carries request/turn/tool receipts. A submitted speech request
is not evidence that the user heard it; the verification probe also checks actual
non-silent audio and the matching final transcript.

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
