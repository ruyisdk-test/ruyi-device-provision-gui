# oh-my-ruyi

A PySide6 management frontend for `ruyi`, with package manager version control
and device provisioning in one tabbed application.

The GUI imports `ruyi` as a Python library and drives the same provisioning
flow used by the interactive CLI. It does not reimplement board/image/package
selection logic on its own; those choices come from ruyi metadata and ruyi's
provision strategy plugins.

## Features

- Three top-level tabs for version management, repository management, and device
  provisioning. Repository management is reserved for future work.
- Stable and testing package manager release discovery with API fallback.
- Per-user standalone ruyi downloads and `/usr/local/bin/ruyi` activation.
- Graphical first-install telemetry choices using ruyi's native OOBE flow.
- Single-window provisioning flow with the steps visible on the left.
- Device, variant, image, package version, package confirmation, download,
  storage selection, review, flash, and done steps.
- Double-click activation for device, variant, image, and package choices.
- Download/install output streamed into the GUI, with cancellation and recovery.
- Flash output streamed into the GUI, including `dd status=progress` output.
- GUI handling for ruyi plugin confirmation prompts and `sudo` password input.
- Automatic `fastboot devices` check for fastboot strategies.
- Automatic disk discovery for `dd` targets, with mounted-disk confirmation.

## Requirements

- Linux, macOS, or Windows through WSL2.
- Python 3.11 or newer.
- `uv` for local development.
- A graphical Qt session.
- Python dependencies from `pyproject.toml`:
  - `PySide6`
  - `ruyi`

Install the project and its development dependencies with:

```bash
uv sync --dev
```

`ruyi` is resolved from the configured Python package index. A sibling
`../ruyi` source checkout is not required. Developers who intentionally want
to run against a local ruyi checkout can opt in for that command without
changing the project configuration or lock file:

```bash
uv run --with-editable /path/to/ruyi oh-my-ruyi
```

The GUI itself does not depend on `lsblk` or the external `git` command.
Linux disk discovery uses `/sys/block`, `/dev`, and ruyi's existing
`/proc/self/mounts` parsing. macOS disk discovery uses `diskutil` plist output.
Native Windows storage flashing is not supported; run the GUI inside WSL2 and
attach USB devices with `usbipd` / usbip.

Flash strategies may require commands depending on the selected device image:

- `dd` for dd-based flashing.
- `sudo` when a strategy retries a failed command with elevated privileges.
- `fastboot` for fastboot-based flashing and the review-page device check.
  On macOS, fastboot device discovery is best-effort and may not find devices
  depending on drivers, permissions, and USB mode.

## Run

```bash
uv run oh-my-ruyi
```

Equivalent module entry point:

```bash
uv run python -m oh_my_ruyi
```

The GUI must be started from a graphical session. Running from a plain TTY or
SSH session without `DISPLAY` or `WAYLAND_DISPLAY` will fail at Qt startup.

## Metadata

The Device page is populated from the same metadata used by
`ruyi device provision`. The configured ruyi repository must contain device
provisioning entities such as:

- `entities/device`
- `entities/device-variant`
- `entities/image-combo`

If the current metadata repository has no device provisioning data, the Device
page shows the available entity types and exposes an `Update metadata` button.
That button runs the repository sync portion of ruyi's update flow through
ruyi's Python API.

## Package Manager Versions

The Version Management tab reads the latest stable and testing releases from
`https://api.ruyisdk.cn/releases/latest-pm`. If that endpoint fails or returns
invalid data, it falls back to
`https://ruyisdk.org/data/api/api_ruyisdk_cn/releases_latest_pm.json`.

Downloaded binaries are stored as `ruyi-<version>` under
`~/.local/share/oh-my-ruyi/versions`. Architecture suffixes such as `.amd64`
are not retained. The active version is not stored in a separate state file;
it is derived directly from the target of `/usr/local/bin/ruyi`.

The version page separates available downloads from locally downloaded
versions. A custom URL can be added for the current application session when
its filename matches `ruyi-<semver version>.<arch>`; refreshing the API catalog
does not remove these transient entries. Custom URLs are not persisted and are
downloaded only after selecting the row and pressing `Download`. Select a
transient entry and press `Remove` to remove it from the current session.
Downloading always opens a URL-selection dialog, including when only one mirror
is available. The same dialog shows byte progress after confirmation, closes on
success, and retains any failure message so another URL can be selected and
retried. `Cancel` remains available during transfer and aborts the active
response while removing partial download data. The dialog closes immediately
so the rest of the application remains usable while cleanup finishes.

Activation may require a sudo password. If `/usr/local/bin/ruyi` already exists
and is not a symlink managed by Oh My Ruyi, the GUI asks before replacing it.
After confirmation, the existing path is moved to `ruyi.bak` or the next free
numbered backup before the managed symlink is installed.

Downloaded versions can be switched with `Activate`, removed with `Delete`, or
disconnected from `/usr/local/bin/ruyi` with `Deactivate`. The active binary
must be deactivated before it can be deleted. Deactivation removes only the
managed symlink and does not restore or delete backups. The local table shows
only binaries compatible with the host CPU architecture, determined from each
executable header. It reports channel, active state, and size in separate
columns. Versions matching the API catalog are marked `Latest`; transient Add
URL entries do not affect that note. Add URL also rejects architecture suffixes
that do not match the host. `Browse` reveals the selected binary in the system
file manager, while the local `Refresh` button rescans the versions directory.
The local panel also reports whether the first `ruyi` found through the GUI
process's `PATH` resolves through the managed activation link or is
missing/shadowed by another installation.

If `/usr/share/ruyi/config.toml` contains
`installation.externally_managed = true`, the version tables remain visible but
all version-management controls are disabled. In that configuration, ruyi
version changes are delegated to the system package manager.

When `~/.local/state/ruyi/telemetry/installation.json` is absent after
activation, the GUI presents ruyi's first-install telemetry choices. It then
runs the activated binary's `ruyi telemetry status` command in a pseudo-terminal
with those answers, allowing ruyi itself to create installation state and apply
the selected `on`, `local`, or `off` mode.

For local metadata development, point ruyi at a metadata tree that contains
the device entities. Example:

```toml
[repo]
local = "/path/to/ruyinews"
```

## Provisioning Flow

The GUI mirrors the CLI flow in a form that can be driven from one window:

1. Prepare or sync the ruyi metadata repository.
2. Select device.
3. Select variant.
4. Select image combo.
5. Customize package versions when ruyi says customization is useful.
6. Confirm packages.
7. Download and install package artifacts.
8. Select storage targets if the strategy needs host block devices.
9. Review the actions ruyi will perform.
10. Flash using the selected strategy.
11. Show the final status and post-install message.

## Storage Selection

For dd-based strategies, the Storage step lists disk targets in a combo box.
On Linux and WSL2, the automatic disk list is built from `/sys/block` and
`/dev`:

- Whole disks are listed.
- Loop, RAM, zram, device-mapper, and md devices are skipped.
- Unmounted disks are sorted first.
- Mounted disks are sorted after unmounted disks and marked as `mounted`.
- Each group is sorted by device name.

The `...` button opens a `QFileDialog` rooted at `/dev`. Paths selected through
the dialog are appended after the automatic disk list, in the order selected.

On macOS, the automatic list is built from `diskutil list -plist` and
`diskutil info -plist`, and whole disks are offered as raw disk paths such as
`/dev/rdiskN`. Disk discovery runs in a worker thread so slow `diskutil`
queries do not freeze the window. APFS physical stores are associated with
their synthesized containers and mounted volumes.

If the selected disk or one of its partitions is mounted, the GUI shows a red
warning and requires an explicit confirmation checkbox before continuing. On
Linux, the check also follows device-mapper/holder relationships so mounts
through LUKS, LVM, or RAID layers are not silently missed. The check uses
ruyi's mount parser for `/proc/self/mounts`; macOS uses `diskutil` metadata.
The selected target's device identity is recorded when Storage is committed
and verified again immediately before flashing, so a replaced `/dev` path is
not silently reused. The flash worker repeats this validation at every actual
`dd` process launch, including after sudo confirmation and password entry.

## Flashing

Flash strategies are provided by ruyi metadata plugins. The GUI does not hard
code board-specific flashing commands.

During flashing, the worker intercepts ruyi plugin subprocess calls so output
can be displayed in the Flash log. For `dd`, the GUI adds `status=progress`
unless the command already specifies a `status=` option.

If a ruyi plugin asks whether to retry with `sudo`, the GUI shows a confirmation
dialog. If `sudo` is used, the GUI asks for the password and feeds it to
`sudo -S` instead of leaving the process blocked in the terminal.

On flash failure, the Flash page shows recovery actions:

- `Retry flash`
- `Review settings`
- `Start over`

## Tests

Run the smoke tests without a display:

```bash
QT_QPA_PLATFORM=offscreen uv run pytest tests/test_smoke.py -v
```

Compile-check the package:

```bash
uv run python -m compileall -q oh_my_ruyi tests
```

GitHub Actions runs the lockfile, Ruff, compile, package build, and full
offscreen test checks for every push and pull request.

## Project Layout

```text
oh_my_ruyi/
  __main__.py        # module entry point
  app.py             # QApplication bootstrap
  download_child.py  # subprocess entry point for download/install
  host_storage.py    # Linux/WSL2/macOS disk discovery and mount checks
  main_window.py     # single-window Qt UI
  qt_logger.py       # RuyiLogger subclass that emits Qt signals
  ruyi_facade.py     # Qt-free facade over ruyi internals
  state.py           # GUI flow state
  version_manager.py # release discovery, downloads, activation, telemetry
  workers.py         # QThread workers for version, repo, and flash operations
tests/
  test_host_storage.py # platform storage backend tests
  test_version_manager.py # package manager version service tests
  test_smoke.py      # import, UI construction, and targeted regression tests
```

## Status

Alpha. Version management and device provisioning are implemented. The
repository management tab is currently empty.
