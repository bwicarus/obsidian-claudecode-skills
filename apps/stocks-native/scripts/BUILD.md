StocksNative uses an independent bundle `space.bwicarus.stocksnative`, version `0.2.2`, iOS 17+, iPhone and iPad. Its distribution certificate and App Store Connect key can reuse the repository's existing Apple secrets. Reader profiles and targets are never used.

The local source directory is independent. For GitHub CI, place it at `apps/stocks-native/` on an isolated branch and place the workflow at the repository root `.github/workflows/stocks-native.yml`. Do not copy build logs, credentials, runtime configuration, `.build`, or `.git`. The source repository is public.

The workflow runs only when explicitly dispatched or when one of these tags is pushed:

- `stocks-native-simulator-<unique suffix>`: unsigned simulator build only.
- `stocks-native-ipa-<unique suffix>`: simulator build, then create/reuse only the StocksNative bundle/profile and export a signed IPA. Does not upload to TestFlight.
- `stocks-native-testflight-<unique suffix>`: simulator, signed IPA, confirm a separate StocksNative App Store Connect record exists, then upload.

Tag triggers allow an independent branch to build before the new workflow exists on the repository's default branch. Ordinary branch pushes do not start macOS jobs. The workflow has its own concurrency group and does not dispatch or edit the Reader workflow.

Existing repository secret names confirmed read-only: `APPLE_API_ISSUER_ID`, `APPLE_API_KEY_BASE64`, `APPLE_API_KEY_ID`, `APPLE_DIST_P12_BASE64`, `APPLE_DIST_P12_PASSWORD`, `APPLE_TEAM_ID`. Secret values were not retrieved. `APPLE_APP_PROFILE_BASE64` and `APPLE_EXTENSION_PROFILE_BASE64` belong to Reader and are deliberately unused.

On a Mac with Xcode and XcodeGen 2.46.0 installed, `bash scripts/build_ios.sh simulator` compiles without signing. CI additionally validates the independent archive's bundle identity, iPad support and microphone purpose, then verifies its signature before exporting.

Signing uses the existing valid distribution identity, matches it to the Apple team's certificate, and registers the exact new bundle only if absent. The StocksNative App ID has Sign in with Apple enabled as a primary App ID. The workflow reuses only an active distribution profile whose decoded entitlements contain `com.apple.developer.applesignin = [Default]`; otherwise it creates a new StocksNative profile. It never creates/revokes certificates, deletes profiles, or changes Reader capabilities.

Apple separates a registered bundle identifier from an App Store Connect app record. The public API explicitly does not create app records: [Apple Apps API](https://developer.apple.com/documentation/appstoreconnectapi/apps). Before a first TestFlight upload, create a new iOS app in [App Store Connect](https://appstoreconnect.apple.com/apps), with bundle `space.bwicarus.stocksnative`, a unique name (suggested `BW Stocks`), primary language Simplified Chinese, and unique SKU (suggested `stocksnative-2026`). See [Apple's steps](https://developer.apple.com/help/app-store-connect/create-an-app-record/add-a-new-app/).

An exported App Store IPA is an artifact, not directly installable by arbitrary devices. TestFlight requires the separate app record, successful Apple processing and tester access. The workflow upload receipt alone does not prove installation readiness. This pipeline does not create tester groups, invite testers, submit for App Review or change existing TestFlight apps.

Windows checks cover Python syntax and workflow structure. The macOS workflow is the authority for Xcode compilation, certificate validity, profile entitlements, signed archive and IPA export.
