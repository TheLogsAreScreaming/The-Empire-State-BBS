# Intel Mac test builds

Every push to `main`, pull request, or manual workflow dispatch runs **Intel Mac test build** on a GitHub `macos-15-intel` runner.

## Download the latest test build

1. Open [Intel Mac test build Actions](https://github.com/TheLogsAreScreaming/The-Empire-State-BBS/actions/workflows/intel-mac-test-build.yml).
2. Choose a **successful** run on `main` (or the branch you intend to test).
3. Under **Artifacts**, download **The-Empire-State-BBS-Intel-Mac-Test**.
4. Unzip the artifact, then open `The-Empire-State-BBS-Intel-Test.dmg`.
5. Drag **The Empire State BBS.app** into Applications.
6. In Finder, Control-click the app and choose **Open** if macOS prompts. If needed, use **System Settings → Privacy & Security → Open Anyway** for this app only.

The test DMG is **unsigned and unnotarized**. Do not disable Gatekeeper globally.

## What automated checks establish

The Intel workflow runs unit tests, builds an x86_64 app, launches its bundled executable in smoke-test mode, mounts the generated DMG, checks the Applications shortcut, and verifies its SHA-256 checksum. This does **not** verify normal Finder launch on your Mac, live Meshtastic TCP operation, or RF delivery.

## Manual checks before a public beta

- Launch and quit normally in Finder, without force quit.
- Open the NYC dashboard, Grapevine, Bulletin, Locals, Connection, and Settings. Check font readability and window layout.
- Try radio-free demo mode; it must not claim a real connection.
- Configure your own TCP host locally; never commit private radio IP addresses.
- Test TCP disconnect/reconnect and confirm messages are not automatically resent.
- If you have only one radio, mark two-node RF delivery as **not tested**.

Artifacts expire after **14 days**. This workflow does not publish a GitHub Release or require an Apple Developer certificate.
