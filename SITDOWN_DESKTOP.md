# The Sitdown BBS desktop preview

This is an **initial functional desktop launcher**, not a completed macOS app release.

## Run locally

Install Python 3 with Tkinter and dependencies from `requirements.txt`.
Copy `example_config.ini` to `config.ini`, configure your BBS, then run:

```sh
python sitdown_desktop.py
```

Use **Demo mode** (Connection › Options) to explore the UI without connecting a radio. The window layout and design system are documented in `docs/UI_DESIGN.md`. The radio Start/Stop buttons launch and terminate the existing `server.py` process with serial or TCP arguments. This does not modify upstream BBS behavior.

**Current limitations:** 'Server running' means the process is alive; radio state is reported separately by the radio health indicator, and it covers the local link only, **not** over-the-air delivery. The welcome message goes out over the radio only when that option is turned on in Settings. Backups are ZIP files; restoring one replaces only the files it contains and first saves a safety copy. The Intel Mac DMG is unsigned and unnotarized, so macOS asks for approval on first launch.

Radio commands supported by this fork are in `SITDOWN_COMMANDS.md`. The current colors are the NYC charcoal and crown-orange palette listed in `README.md`.

Do not share `config.ini` or backups publicly. Preserve upstream GPLv3 licensing and acknowledgements.
