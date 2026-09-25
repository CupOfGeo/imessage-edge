# Packaging notes — imessage-edge as a Homebrew formula

How this app is shipped to the Mac that runs it. The `README.md` covers
day-to-day dev (`uv sync && uv run python -m imessage_edge`); this file
covers how end users install and how we cut new releases.

## Distribution

Shipped as a Homebrew formula in the public tap
[CupOfGeo/homebrew-tap](https://github.com/CupOfGeo/homebrew-tap), at
`Formula/imessage-edge.rb`. The formula pulls the source tarball for a
tagged release of [CupOfGeo/imessage-edge](https://github.com/CupOfGeo/imessage-edge)
on GitHub. Install on any Mac:

```bash
brew tap CupOfGeo/tap
brew install CupOfGeo/tap/imessage-edge
brew services start CupOfGeo/tap/imessage-edge
```

Brew autogenerates `IMESSAGE_EDGE_TOKEN` into `/opt/homebrew/etc/imessage-edge/config.env`
on first install; user must fill in `ORCHESTRATOR_URL` + `IMESSAGE_INBOUND_TOKEN`
before starting the service. Caveat printed during install spells this out.

## Architecture decisions worth remembering

A few things in the formula look weird; here's why.

### Renamed Python stub: `python3.14` → `imessage-edged`
Inside the brew venv, the Python interpreter is renamed so macOS Full
Disk Access shows the daemon as `imessage-edged` rather than a generic
`python` / `python3.14`. Compat symlinks (`python`, `python3`,
`python3.14`) point at it so dep CLIs (uvicorn etc.) still resolve their
shebangs. The daemon's own entry-script shebang was rewritten to bypass
the symlink so launchd execs `imessage-edged` directly (TCC attributes
FDA grants to the path used to exec, not the resolved Mach-O).

### `--copies` venv (not symlink)
Built with `python -m venv --copies` so the stub is a real Mach-O copy,
not a symlink to the shared Homebrew framework binary. Granting FDA
scopes access to *this* daemon's binary alone, not every brew-managed
Python 3.14 invocation on the system.

### No bash launcher
launchd runs the `imessage-edge` Python entry point directly. An earlier
attempt put a bash wrapper in between (to source `config.env`) but
macOS's "responsible process" attribution made TCC deny the chat.db
read. Now the service block sets `IMESSAGE_EDGE_CONFIG=/opt/homebrew/etc/imessage-edge/config.env`
in launchd's `EnvironmentVariables`; pydantic-settings reads the env
file from that path on its own.

### Manual venv (not `virtualenv_install_with_resources`)
The formula builds the venv by hand so it can pass `--copies` (see
above) before installing the package with `pip_install_and_link`.

### `[build-system] = hatchling`
Required so `pip install .` registers the `imessage-edge` console
script. Without a build backend, pip silently skips entry points.

### Config template in `pkgshare`, not `etc`
`config.env.example` ships at `/opt/homebrew/share/imessage-edge/`,
*not* `/opt/homebrew/etc/imessage-edge/`. Brew refuses to overwrite
files in `etc` on reinstall (treats them as user-modifiable config),
which would make every reinstall fail. `pkgshare` lives in the cellar
and gets recreated each install.

## Cutting a new release

```bash
# 1. Bump version (read at runtime via importlib.metadata, so this is
#    the only place it lives)
$EDITOR pyproject.toml      # version = "X.Y.Z"
uv lock                     # refresh the lockfile's own-package entry

# 2. Commit, tag, push. Use an annotated tag: its message becomes the
#    GitHub release notes, so write them for a public reader.
cd ~/Code/imessage-edge
git add -A
git commit -m "vX.Y.Z: <what changed>"
git push
git tag -a vX.Y.Z
git push origin vX.Y.Z

# 3. Publish the public snapshot + GitHub release. Builds a filtered
#    single commit from the tag (exclude list: scripts/snapshot-exclude.txt),
#    refuses to ship if any internal string survives, pushes it to
#    github.com/CupOfGeo/imessage-edge, creates the release, and prints
#    the tarball sha256 for the formula. Run with --dry-run first.
#    The formula must hash the *GitHub* tarball, not the Forgejo one:
#    the snapshot excludes internal files, so the digests differ.
scripts/publish-snapshot.sh --dry-run   # tag is read from HEAD
scripts/publish-snapshot.sh

# 4. Update formula in the tap repo
$EDITOR ~/Code/homebrew-tap/Formula/imessage-edge.rb
#   - url:     bump version
#   - sha256:  paste the new digest

# 5. (Optional) refresh transitive Python dep pins
brew tap CupOfGeo/tap ~/Code/homebrew-tap   # if not already tapped from local path
brew update-python-resources CupOfGeo/tap/imessage-edge

# 6. Push the tap
cd ~/Code/homebrew-tap
git add Formula/imessage-edge.rb
git commit -m "imessage-edge: vX.Y.Z"
git push

# 7. Local reinstall + re-grant FDA + restart
brew services stop CupOfGeo/tap/imessage-edge
brew uninstall imessage-edge
brew install CupOfGeo/tap/imessage-edge
# Grant FDA to the new path: System Settings -> Privacy & Security -> FDA
#   /opt/homebrew/Cellar/imessage-edge/X.Y.Z/libexec/bin/imessage-edged
# (Old version's entry is now orphaned — `-` to remove.)
brew services start CupOfGeo/tap/imessage-edge
```

Steps 3–6 are the part the release action is meant to absorb: it runs
`publish-snapshot.sh` on tag push, then commits the formula bump to the
tap. Until then steps 4–6 are manual.

The tap repo on GitHub is the source of truth; the working copy at
`/opt/homebrew/Library/Taps/cupofgeo/homebrew-tap/` can be edited
in-place during iteration, then copied to `~/Code/homebrew-tap/` and
pushed.

## Uninstall

Homebrew has no uninstall hook, and `brew uninstall` does not stop a
running service. Stop it first or launchd keeps the old process alive
(pointing at a Cellar path that no longer exists):

```bash
brew services stop CupOfGeo/tap/imessage-edge
brew uninstall imessage-edge
```

If you already uninstalled and the daemon is still running (`lsof -i :8765`),
`brew services cleanup` boots out launchd jobs for formulae that are no
longer installed. Config in `/opt/homebrew/etc/imessage-edge/` survives
uninstall by design; remove it by hand if you want a clean slate.

## Full Disk Access

The daemon needs FDA to read `~/Library/Messages/chat.db`. macOS only
exposes broad FDA — no per-file granular scoping. Apple's design.

**Grant target:**
```
/opt/homebrew/Cellar/imessage-edge/<version>/libexec/bin/imessage-edged
```
Toggle ON in System Settings → Privacy & Security → Full Disk Access.
The list will show it as `imessage-edged`.

**Re-grant on every brew upgrade.** macOS records FDA against the
resolved versioned cellar path + the binary's CDHash. Each rebuild
rotates the hash and changes the path; TCC silently drops the grant.
This is a fundamental limitation of ad-hoc-signed binaries (Homebrew's
Python isn't Apple Developer ID signed). The "right" fix is signing the
binary with a stable identity, but that's a bigger lift.

**Verifying FDA in isolation.** Anything spawned from your shell (including
`osascript … do shell script`) inherits the terminal's FDA via macOS's
"responsible process" attribution, so it passes even when the daemon is
denied. The only honest test is to have launchd run the binary, which is
what the service does:

```bash
B=/opt/homebrew/Cellar/imessage-edge/<version>/libexec/bin/imessage-edged
launchctl submit -l fdatest -o /tmp/fdatest.out -e /tmp/fdatest.out -- "$B" -c \
  "import sqlite3,os; print('FDA-OK', sqlite3.connect('file:'+os.path.expanduser('~/Library/Messages/chat.db')+'?mode=ro', uri=True).execute('select count(*) from message').fetchone()[0])"
sleep 3; tail -1 /tmp/fdatest.out; launchctl remove fdatest
```

`FDA-OK <count>` means the grant is real; `unable to open database file`
means it isn't (wrong row toggled, stale entry for an old Cellar path, or
not added yet). No service restart is needed to run this. Once it passes,
`brew services restart CupOfGeo/tap/imessage-edge` and check
`/health` reports `chat_db_readable: true`.

## Known issues

- **FDA list accumulates orphans.** Every reinstall leaves a stale FDA
  entry for the previous cellar version (different CDHash, brew can't
  clean these up). Manual cleanup: System Settings → FDA → click old
  entry → `−` button → authenticate.
- **Tap and source repo are decoupled.** The GitHub `homebrew-tap`
  repo holds the formula; this repo holds the source. Until the release
  action updates the tap, bumping the source version means manually
  updating the formula's `url` + `sha256` and pushing both repos.
