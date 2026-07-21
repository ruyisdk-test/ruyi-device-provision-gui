# oh-my-ruyi

[![License](https://img.shields.io/github/license/ruyisdk-test/ruyi-device-provision-gui)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Latest tag](https://img.shields.io/github/v/tag/ruyisdk-test/ruyi-device-provision-gui?label=latest%20tag)](https://github.com/ruyisdk-test/ruyi-device-provision-gui/tags)

(如意宝 in Chinese)

Newly designed [ruyi-device-gui](https://github.com/weilinfox/ruyi-device-gui).

A PySide6 management frontend for `ruyi`, with package manager version control
and device provisioning in one tabbed application.

The GUI imports `ruyi` as a Python library and follows the same provisioning
flow as the interactive CLI. Device, image, package, and flashing choices come
from ruyi metadata and its provision strategy plugins.

## Features

- Version management, repository management, device provisioning, and runtime
  information in four top-level tabs.
- Preset and user-local repository management through ruyi's configuration API.
- Stable and testing ruyi release discovery with a fallback source.
- Per-user standalone ruyi downloads and activation of the managed command.
- An optional first-use setup flow for downloading the latest stable ruyi,
  choosing a RuyiSDK metadata mirror, and updating it.
- First-install telemetry choices using ruyi's native flow.
- A single-window provisioning flow with visible steps and recovery actions.
- Device, variant, image, package version, package confirmation, download,
  storage, review, flash, and done steps.
- Streaming Rich output with cancellation, progress, and plugin prompt handling.
- Automatic `fastboot devices` checks for fastboot-based strategies.
- Disk discovery with mounted-device warnings and explicit confirmation.
- Chinese localization for the GUI and ruyi output when routed through
  `zh_CN.UTF-8`.

## Requirements

- Linux, macOS, or Windows through WSL2.
- Python 3.11 or newer.
- `uv` to install and run the project.
- A graphical Qt session.

Install the project and its dependencies with:

```bash
uv sync
```

The GUI itself does not support native Windows storage flashing. Run it inside
WSL2 and attach USB devices with `usbipd` / `usbip`.

Depending on the selected image, flashing may also require:

- `dd` for dd-based strategies.
- `sudo` when elevated privileges are needed.
- `fastboot` for fastboot-based strategies and device checks.

## Run

Start the GUI from a graphical session:

```bash
uv run oh-my-ruyi
```

Equivalent module entry point:

```bash
uv run python -m oh_my_ruyi
```

Running from a plain TTY or an SSH session without `DISPLAY` or
`WAYLAND_DISPLAY` will fail at Qt startup.

## First-use Setup

On first use, Oh My Ruyi offers a setup flow when no ruyi telemetry installation
state exists, no `ruyi` command outside the application's Python environment is
found on `PATH`, and the Oh My Ruyi data directory does not exist. On Linux these
paths are under `~/.local/`; on macOS they follow ruyi's `~/Library/Application
Support/` locations. The flow can be exited at any time.

It offers to download and activate a compatible ruyi release at
`/usr/local/bin/ruyi`, preferring the latest stable release when available. You
can skip that download and continue to choose the default `ruyisdk` metadata
mirror. After the selected mirror is updated, the GUI opens the About tab.
Skipping or exiting does not record completion, so the setup remains available on
a later launch while the same conditions hold.

## Localization

The GUI selects its locale at startup and currently routes Chinese translations
only for `zh_CN.UTF-8`. Restart the GUI after changing the system locale. Other
locales remain in English unless both Oh My Ruyi and `ruyi` provide matching
translation resources.

The selected locale is also propagated to Qt standard controls, imported ruyi
APIs, and child processes. Repository IDs, URLs, paths, package names, and
device names remain unchanged.

## Metadata

The Device page uses the same metadata as `ruyi device provision`. The active
ruyi repository must contain device provisioning data, including entities for
devices, device variants, and image combos.

If the metadata does not contain those entities, the Device page shows the
available entity types and provides an `Update metadata` action.

## Package Manager Versions

The Version Management tab separates available releases from versions already
downloaded on the computer. A custom release URL can be added for the current
session when its filename matches `ruyi-<semver version>.<arch>`.

Downloads open a URL-selection dialog and show byte progress after confirmation.
Failed downloads retain their output so another URL can be selected and retried.
`Cancel` aborts the active transfer and removes partial download data.

Activation may require a sudo password. If the managed activation path already
contains an unmanaged file or symlink, the GUI asks for confirmation before
preserving it as a numbered `.bak` backup.

Downloaded versions can be activated, deleted, or deactivated. An active binary
must be deactivated before it can be deleted. The local panel also reports when
the first `ruyi` found on `PATH` is missing or shadowed by another installation.

If the system ruyi installation is configured as externally managed, version
controls remain visible but are disabled and version changes are delegated to
the system package manager.

## Provisioning Flow

The GUI mirrors the CLI flow in one window:

1. Prepare or sync the ruyi metadata repository.
2. Select a device, variant, and image combo.
3. Customize package versions when useful alternatives exist.
4. Confirm and download packages.
5. Select storage targets when required by the image.
6. Review the actions ruyi will perform.
7. Flash using the selected strategy.
8. Review the final status and post-install message.

Double-clicking a device, variant, image, or package choice advances when the
current step is ready.

## Storage Safety

For dd-based images, the Storage step lists whole-disk targets and marks mounted
targets. A mounted disk or partition requires an explicit confirmation before
the flow can continue.

The selected target is checked again immediately before flashing. If the target
has changed, disappeared, or become mounted, flashing stops and the target must
be reviewed again.

## Flashing

Flash strategies are provided by ruyi metadata plugins; the GUI does not hard
code board-specific commands. During flashing, plugin output and progress are
shown in the Flash page.

When a strategy requests a retry with `sudo`, the GUI shows a confirmation dialog
and securely feeds the entered password to `sudo -S`.

On flash failure, the Flash page provides `Retry flash`, `Review settings`, and
`Start over` actions.

## Development

Contributor setup, architecture, local ruyi and metadata development, locale
extension, testing, CI, and packaging are documented in the
[Development Guide](docs/development-guide.md). Coding agents should read the
repository's [agent development context](AGENTS.md) before making changes.

## Status

Alpha. Version management, repository management, and device provisioning are
implemented. Repository source edits that cannot be expressed by the installed
ruyi API are intentionally unavailable.
