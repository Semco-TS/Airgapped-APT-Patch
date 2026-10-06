# Airgapped APT Patch

Airgapped APT Patch moves Ubuntu package update data through a Windows PC that has internet access and can also reach the target server over SSH. The server does not need direct access to the internet, but it must be reachable from the PC while the update is run. This is not a tool for a server that is completely disconnected from the PC.

## How It Works

For each update round, the application asks `apt-offline` on the server to create a `.sig` request file, then retrieves that file over SFTP. The request lists the files needed by the server. The PC downloads those files from their listed mirror URLs, checks their recorded sizes and supported hashes when present, and uploads the validated files to the server.

By default, the application performs two rounds:

1. **Update:** refresh the server's package lists using an `apt-offline` update request.
2. **Upgrade:** create a request for package updates, transfer the required files, install them with `apt-offline`, then run `apt-get upgrade --no-download` on the server. The upgrade type can be changed to `dist-upgrade` in the options.

Before the upgrade install, the application can show a package summary and ask for confirmation. After a successful run, cleanup can remove the request and transferred files from the PC and/or server. The application also checks whether the server reports that a reboot is required.

### Package Verification

The `.sig` file is an `apt-offline` download request; it is not a cryptographic signature created by this application. Before upload, the application checks each downloaded file against the size and supported digest recorded in that request, when supplied. A mismatch prevents that file from being accepted as a successful download.

Repository authenticity is still the responsibility of APT on the server. Keep the server's APT sources and trusted signing keys configured correctly; this application does not create signing keys or replace APT's repository-signature checks.

## Operating Systems

- **Target server:** Ubuntu is the primary target. Debian and Ubuntu-derived systems may also work when they use APT, have a compatible `apt-offline` installation, and provide the expected APT commands. Other package-management families, such as RPM-based distributions, are not supported.

- **Controller PC:** Windows is the intended desktop platform. The Python source may also run on Linux or macOS where a supported Python/PySide6 environment is available and the PC can reach both the internet mirrors and the server over SSH; these platforms have not been verified by this project.


## Requirements

On the server:

- SSH access from the controller PC.
- `apt-offline` installed and usable by the configured account.
- Root access or permission to run the required commands with `sudo`.
- Working APT repositories and signing-key configuration.

On the Windows controller:

- Internet access to the mirror URLs listed in the request files.
- SSH network access to the server.
- For running from source, Python and the packages in `requirements.txt`.

The first connection to an unknown SSH server displays its host-key fingerprint. Verify the fingerprint through a trusted channel before accepting it. The host key is then saved for later connections. SSH and sudo passwords are not saved in the settings file; other connection and workflow options are saved in the user's home directory.

## Run From Source

From the project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python apt_remote_update.py
```

Enter the server connection details, test the connection, and then run the update. The default options refresh package lists, use `upgrade`, ask before installation, and clean up transferred files after success. `apt-get clean` is optional and clears the server's local APT package cache; it is not enabled by default.

## Build a Windows Executable

Install PyInstaller into the same environment as the application, then build from the project directory:

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name Airgapped-APT-Patch apt_remote_update.py
```

PyInstaller's default output is a folder-based build under `dist\Airgapped-APT-Patch\`. Test the executable on a clean Windows machine that does not have the project environment installed. Keep the generated `build\` and `dist\` folders out of source control unless the project specifically intends to distribute build artifacts.

## Test-Only Safety Notes

- Run initial trials against a disposable Ubuntu/Debian VM with a recoverable snapshot.
- Confirm the SSH host-key fingerprint independently before trusting it.
- Review the package summary and server logs. The application ultimately runs APT with automatic confirmation (`-y`); the pre-install confirmation prompt is enabled by default but can be disabled.
- Test cancellation, interrupted transfers, cleanup settings, stale or unavailable mirror files, and both upgrade types before considering broader use.
- This project has not been certified for unattended fleet updates, or every Ubuntu/Debian release and repository configuration.
