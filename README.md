# ruyi-device-provision-gui

A PySide6 single-window frontend for `ruyi device provision`.

The GUI imports `ruyi` as a Python library and drives the same provisioning
flow used by the interactive CLI. It does not reimplement board/image/package
selection logic on its own; those choices come from ruyi metadata and ruyi's
provision strategy plugins.

## Features

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

This project uses the sibling `../ruyi` checkout as an editable dependency:

```bash
cd /path/to/ruyisdk/ruyi-device-provision-gui
uv sync --dev
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
uv run ruyi-device-provision-gui
```

Equivalent module entry point:

```bash
uv run python -m ruyi_device_provision_gui
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

For local metadata development, point ruyi at a metadata tree that contains
the device entities. Example:

```toml
[repo]
local = "/home/hachi/Documents/ruyisdk/ruyisdk-ruyisdk-website/news/ruyinews"
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
uv run python -m compileall -q ruyi_device_provision_gui tests
```

## Project Layout

```text
ruyi_device_provision_gui/
  __main__.py        # module entry point
  app.py             # QApplication bootstrap
  download_child.py  # subprocess entry point for download/install
  host_storage.py    # Linux/WSL2/macOS disk discovery and mount checks
  main_window.py     # single-window Qt UI
  qt_logger.py       # RuyiLogger subclass that emits Qt signals
  ruyi_facade.py     # Qt-free facade over ruyi internals
  state.py           # GUI flow state
  workers.py         # QThread workers for repo sync and flashing
tests/
  test_host_storage.py # platform storage backend tests
  test_smoke.py      # import, UI construction, and targeted regression tests
```

## Status

Alpha. The implementation is intended to stay close to ruyi's real
`device provision` behavior while providing a GUI around the interactive steps.
