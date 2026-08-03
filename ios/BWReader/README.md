# BWReader iOS

This directory is the release source for the single iPad app that contains both
the native BWReader experience and the Safari Web Extension.

- App bundle: `space.bwicarus.bwreader2`
- Extension bundle: `space.bwicarus.bwreader2.Extension`
- Project source: `project.yml` (XcodeGen 2.46.0)
- Release workflow: `.github/workflows/safari-extension-ios.yml`

The seven Swift sources in `App/` were migrated byte-for-byte from
`C:\iCloudDrive\BWReaderNative.swiftpm`. The AppIcon pixels are unchanged, but
its release copy is encoded as RGB so App Store validation does not reject an
otherwise opaque RGBA image. After this migration, changes intended for
TestFlight belong here; the old Swift Playgrounds package remains a local
rollback/reference copy.

The Chromium extension manifest remains the source of truth. CI runs
`extensions/bw-reader-webext/package_safari.py`, expands its verified package
into `Extension/Resources/`, generates `BWReader.xcodeproj`, and builds the app.
Generated resources and the Xcode project are intentionally not committed.
