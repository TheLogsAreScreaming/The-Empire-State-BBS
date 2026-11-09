# Intel Mac beta release process

The Empire State BBS has two build workflows:

- **Intel Mac test build** runs on `main` and pull requests, packaging an unsigned test DMG and checking the bundled executable.
- **Publish Intel Mac Release** can build a versioned x86_64 DMG and SHA-256 manifest on a `v*` tag or manual dispatch. **Do not trigger this workflow until release approval.**

## Preparing a release

`vX` below means the version being prepared, such as `v0.1.0-beta.9`. It is a **candidate only** until a GitHub Release is actually published.

### Release gates

- [ ] Merge reviewed feature, UI, documentation, and screenshot PRs; no unresolved merge conflicts.
- [ ] Confirm **The Empire State BBS checks** and **Intel Mac test build** pass on the final `main` SHA.
- [ ] Download the latest Intel Mac test artifact and verify a normal Finder launch and clean quit on an actual Intel Mac.
- [ ] Review the NYC charcoal/orange dashboard and demo screenshot for readable text, window sizing, and accurate status indicators.
- [ ] Open Grapevine, Bulletin, Locals, Connection and Settings repeatedly; verify no duplicate windows.
- [ ] In demo mode, confirm no radio connection or message delivery is falsely claimed.
- [ ] With an available radio, verify TCP connect, disconnect, reconnect, and safe draft persistence; record any untested two-node RF delivery explicitly.
- [ ] Confirm attribution, GPLv3 licensing, README installation instructions, and screenshot disclaimer remain correct.
- [ ] Approve publication of `vX` explicitly.

### Publish only after approval

1. Open **Actions → Publish Intel Mac Release → Run workflow** on `main`.
2. Enter `vX` only if that tag does not already exist.
3. Verify the release workflow succeeds, including the read-only DMG mount and SHA-256 check.
4. Confirm GitHub Releases contains `The-Empire-State-BBS-Intel-Mac-vX.dmg` and `SHA256SUMS.txt`.
5. Verify the downloaded DMG checksum and test the installed app again.
6. Mirror the same DMG and `SHA256SUMS.txt` to `TheLogsAreScreaming/The-Empire-State-BBS-Releases` as a pre-release, then confirm the digests match.

## Safety and verification limits

Builds are **unsigned and unnotarized**. Approve trusted builds through macOS **System Settings → Privacy & Security**; never disable Gatekeeper globally.

Unit tests, bundled-app smoke tests, and successful TCP checks do **not** prove over-the-air delivery, recipient acknowledgement, or testing with a second radio. Document those limitations in release notes. A release must not silently retransmit drafts on reconnection.
