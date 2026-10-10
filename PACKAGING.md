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

### A signed launcher app holds Full Disk Access, not Python
`ImessageEdge.app` wraps a small C program (`launcher/imessage-edged.c`)
that launchd runs. It spawns the venv's python as a child, and macOS
attributes the child's chat.db reads to the launcher: TCC checks the
*responsible* process, and a child inherits its parent's. Python itself
gets no grant, so `launchctl submit … python -c <code>` can't borrow FDA.
(This is also why an earlier bash wrapper failed: bash was the
responsible process and had no grant.)

The launcher takes no arguments and ignores its environment, because
`launchctl setenv` reaches every job launchd starts for the user. The
python path and `IMESSAGE_EDGE_CONFIG` are compiled in (the stable
`/opt/homebrew/opt` paths, so Apple Silicon and the default prefix only),
`HOME` comes from the user database, and python runs with `-I` so
`PYTHON*` variables are ignored. A config path taken from the environment
would let anyone point the daemon at their own `ORCHESTRATOR_URL`.

### Built and notarized at release time, not by the formula
A Homebrew install can't sign with our Developer ID, so
`launcher/build.sh` builds the bundle on the release Mac, signs it with
the Developer ID Application certificate and the hardened runtime (dyld
then ignores `DYLD_*`), notarizes and staples it. `publish-snapshot.sh`
attaches the zip to the GitHub release and writes its url + sha256 into
the formula's `launcher` resource. The formula only unpacks it.

Developer ID is what makes the grant survive upgrades: for a signed
bundle, TCC keys the grant on its code requirement (bundle id + Team ID),
not on the path or hash. An ad-hoc copy claiming the same bundle id gets
nothing.

What it does not stop: the Cellar is writable by your user, so a process
running as you can still edit the daemon's site-packages or replace the
launcher, and the change takes effect on the next restart. Closing that
needs a root-owned install, which Homebrew doesn't do.

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

# 3. Publish the public snapshot + GitHub release, then bump the tap.
#    Needs, on this Mac: the Developer ID Application certificate and a
#    `notary` notarytool profile (`xcrun notarytool store-credentials`;
#    the Team ID is the OU of the certificate subject, not the ID in its
#    name). It builds + notarizes the launcher before pushing anything.
#    Builds a filtered single commit from the tag (exclude list:
#    scripts/snapshot-exclude.txt), refuses to ship if any internal string
#    survives or the working tree is dirty, pushes it to
#    github.com/CupOfGeo/imessage-edge, creates the release, then rewrites
#    the formula's url + sha256 in github.com/CupOfGeo/homebrew-tap and
#    pushes that too. Run with --dry-run first (stops before any push).
#    The formula hashes the *GitHub* tarball, not the Forgejo one: the
#    snapshot excludes internal files, so the digests differ.
scripts/publish-snapshot.sh --dry-run   # tag is read from HEAD
scripts/publish-snapshot.sh

# 4. (Optional, when Python deps changed) refresh the formula's resource
#    pins, then commit and push the tap by hand
brew tap CupOfGeo/tap ~/Code/homebrew-tap   # if not already tapped from local path
brew update-python-resources CupOfGeo/tap/imessage-edge

# 5. Local upgrade + restart (the FDA grant carries over)
brew update
brew upgrade CupOfGeo/tap/imessage-edge
brew services restart CupOfGeo/tap/imessage-edge
```

Step 3 is everything a release action would run on tag push; it is
manual for now.

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

**Grant target, once:**
```
/opt/homebrew/opt/imessage-edge/libexec/ImessageEdge.app
```
In System Settings → Privacy & Security → Full Disk Access, click `+`,
press ⌘⇧G, paste the path above and pick `ImessageEdge.app`. The grant
survives `brew upgrade` (see above), so there's nothing to redo after
a release.

**Verifying FDA in isolation.** Anything spawned from your shell (including
`osascript … do shell script`) inherits the terminal's FDA via macOS's
"responsible process" attribution, so it passes even when the daemon is
denied. The only honest test is to have launchd run the binary, which is
what the service does. Restart it and check `/health`:

```bash
brew services restart CupOfGeo/tap/imessage-edge
TOKEN=$(sed -n 's/^IMESSAGE_EDGE_TOKEN=//p' /opt/homebrew/etc/imessage-edge/config.env)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/health
```

`"chat_db_readable": true` means the grant is real. `false` means it
isn't (wrong row toggled, or not added yet). Use your bind address if you changed `IMESSAGE_EDGE_BIND_HOST`.

To confirm Python itself can't use the grant, run it under launchd
directly. It must fail with `unable to open database file`:

```bash
P=/opt/homebrew/opt/imessage-edge/libexec/bin/python
launchctl submit -l fdatest -o /tmp/fdatest.out -e /tmp/fdatest.out -- "$P" -c \
  "import sqlite3,os; sqlite3.connect('file:'+os.path.expanduser('~/Library/Messages/chat.db')+'?mode=ro', uri=True).execute('select 1 from message')"
sleep 3; tail -1 /tmp/fdatest.out; launchctl remove fdatest
```
