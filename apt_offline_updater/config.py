import json
import os
from dataclasses import asdict, dataclass


SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".apt_offline_updater.json")
NEVER_SAVE = {"password", "sudo_password"}


@dataclass
class Config:
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""
    key_file: str = ""
    sudo_password: str = ""
    remote_sig_dir: str = "~/apt-offline/sig"
    remote_pkg_dir: str = "~/apt-offline/packages"
    local_sig_dir: str = os.path.join(os.path.expanduser("~"), "apt-offline", "sig")
    local_pkg_dir: str = os.path.join(os.path.expanduser("~"), "apt-offline", "packages")
    refresh_lists: bool = True
    upgrade_type: str = "upgrade"
    confirm_before_install: bool = True
    cleanup: bool = True
    cleanup_remote: bool = True
    cleanup_local: bool = True
    apt_clean: bool = False


def load_settings():
    cfg = Config()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        for key, value in data.items():
            if hasattr(cfg, key) and key not in NEVER_SAVE:
                setattr(cfg, key, value)

    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg):
    try:
        data = {key: value for key, value in asdict(cfg).items() if key not in NEVER_SAVE}

        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            
    except OSError:
        pass