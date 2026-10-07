# Frontend build and delivery

Chat source lives in `apps/presentation/dashboard`. `loopx/web/chat/` is generated,
ignored output, not a second editable source tree. Feature PRs commit source and
validation only; resolving a source merge followed by a build produces one
coherent HTML/JavaScript/CSS set. Do not concatenate competing HTML entrypoints or
asset-retention manifests when resolving an older PR.

## Source development and updates

Prepare Python with `uv sync --extra test`, then run from the repository root:

```sh
uv run --extra test python scripts/chat_bundle.py build --install
uv run --extra test python scripts/chat_bundle.py verify --source
uv run --extra test loopx chat --no-open
```

Builds start with current assets only. Use `build --previous /path/to/prior/chat`
when deliberately carrying a prior local delivery; release and installation
commands select their predecessor automatically.

With frontend dependencies already installed, `npm run build:chat` inside
`apps/presentation/dashboard` rebuilds the packaged UI. That npm entry runs
`scripts/chat_bundle_launcher.mjs`, which selects a Python 3.11+ interpreter the
same way the rest of LoopX does (`LOOPX_PYTHON`, then the installer-recorded
`.loopx-python`, the repository `.venv`, and `python3`/`python`, plus the `py`
launcher on Windows) and then runs the shared `scripts/chat_bundle.py` builder.
Supported Windows installations expose `python.exe` without a usable `python3`
alias, so the entry never assumes the POSIX name; the Windows CI lane rebuilds
the bundle under exactly that condition. `loopx dashboard`'s source development
launcher ensures the packaged backend assets exist before starting Vite. Run the
build again after updating source. A packaged Chat launch rejects missing,
corrupt or stale assets with an actionable rebuild message, including before
reusing an existing service. Explicit development/test `--assets-dir` continues
to support caller-owned assets.

The bundle manifest records the checkout revision, source fingerprints, current
asset set and SHA-256 of every delivered file. Source fingerprints conservatively
include shared TypeScript control-plane contracts. Unrelated changes in that tree
can require a rebuild. Text inputs normalize CRLF so a CI-built bundle remains
valid on Windows; delivered bytes are always hashed exactly. This is integrity
and freshness metadata, not a signature or a replacement for release attestations.

## PR qualification and releases

PR CI builds a clean bundle once, exercises its actual pages in a browser, then
uploads `chat-bundle-<checkout SHA>`. Consumers download that qualified artifact;
both frontend-only and mixed/backend PRs pass through this producer. On pull requests the checkout SHA is GitHub's tested merge commit, which
may differ from the branch head. Generated-file Git cleanliness is no longer a
qualification gate. Browser, integrity and workflow gates remain required.

Release Artifacts checks out the exact release tag and runs:

```sh
python scripts/chat_bundle.py release-build --tag vX.Y.Z --repo loopx-project/loopx
python -m build --sdist --wheel
```

`release-build` requires authenticated `gh` read access and Node/npm. It downloads
the preceding stable version's wheel and `SHA256SUMS`, verifies the checksum, and
carries its current assets into the new bundle. Missing predecessor artifacts or
a checksum mismatch stop the release; prereleases and later versions are
excluded. A repository with no predecessor can bootstrap without history.

Both wheel and sdist include the verified bundle. A normal package build fails
if it is absent/stale; editable installation remains available before a frontend
build. Building a wheel from the sdist requires no Node or network access for the
frontend. Release CI installs both wheel forms outside the checkout and requests
every delivered file through the actual Chat HTTP handler, then runs workspace
browser scenarios against the isolated installed interpreter.

## Upgrade window and rollback

A new delivery retains its own assets and the previous delivery's current assets.
It does not carry the previous delivery's entire retained history. The first
upgrade from an older wheel without provenance metadata retains its bounded asset
directory once. Source installers instead use the current installed snapshot as
the predecessor, so local development build history does not define upgrade
compatibility. Already-built archives can add those resources without Node or a
network connection. A failed build leaves the previous complete output intact.

This supports an old tab requesting a previously unloaded module after one
upgrade. It does not promise backend API compatibility indefinitely, or preserve
resources across arbitrary skipped stable releases for wheel installs. After a
second upgrade, reload older tabs. Rollback reinstalls the previous complete wheel
or selects the previous source snapshot; do not mix one version's HTML with
another version's assets. Existing tabs should reload after rollback.

Validation commands:

```sh
uv run --extra test python -m pytest tests/presentation/test_chat_bundle.py
npm --prefix apps/presentation/dashboard run smoke:chat-upgrade
npm --prefix apps/presentation/dashboard run smoke:personal-workspace-packaged
python scripts/verify_installed_chat.py --python /path/to/installed/environment/bin/python
```

The upgrade browser smoke opens version A, delivers B, imports A's deferred module
from the still-open tab, opens B in a new tab, then proves C retains B and retires A.
It uses disposable assets and the production HTTP handler, without active Goal
state. The packaged workspace smoke separately exercises the actual compiled UI.
