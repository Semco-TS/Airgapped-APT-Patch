import os
import posixpath
import stat
import threading
import time

from .downloads import Downloader, parse_sig
from .errors import Cancelled, EmptySig, WorkflowError
from .remote import APT_ENV, Remote, human


class Throttle:
    def __init__(self, interval=0.1):
        self.interval, self.last = interval, 0.0


    def ready(self):
        now = time.monotonic()

        if now - self.last >= self.interval:
            self.last = now
            return True
        
        return False


class Workflow:
    """UI must provide log, status, progress, and ask callbacks."""

    def __init__(self, cfg, ui, cancel):
        self.cfg, self.ui, self.cancel = cfg, ui, cancel
        self.remote = Remote(cfg, ui.log, cancel, ui.ask)
        self.state = {}
        self.created_remote, self.created_local = [], []


    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()


    def _stage(self, kind, text, progress=None):
        self.stage = f"[{kind}] {text}" if kind else text
        self.ui.status(self.stage)
        self.ui.progress(progress)
        self.ui.log(f"\n=== {self.stage} ===")


    def _preflight(self):
        self.ui.status("Checking server...")
        code, lines = self.remote.run(["apt-offline", "--version"], sudo=True, check=False)
        text = "\n".join(lines).lower()

        if code == 0:
            return lines[-1] if lines else ""
        
        if any(s in text for s in ("incorrect password", "no password was provided",
                                   "a password is required", "sorry, try again")):
            
            raise WorkflowError("sudo rejected the password. Enter the sudo password in the "
                                "'Sudo password' field (or leave it blank to reuse the SSH password).")
        
        if any(s in text for s in ("not in the sudoers", "may not run sudo", "is not allowed to")):
            raise WorkflowError(f"User '{self.cfg.username}' is not allowed to use sudo on the server.")
        
        if code == 127 or "not found" in text:
            raise WorkflowError("apt-offline is not installed on the server "
                                "(install it there from a .deb first).")
        
        raise WorkflowError("Preflight check failed:\n" + "\n".join(lines[-8:]))


    def test(self):
        try:
            self.remote.connect()
            version = self._preflight()

            try:
                _, os_lines = self.remote.run(
                    ["sh", "-c", ". /etc/os-release && echo \"$PRETTY_NAME\""], show=False)
                osname = os_lines[-1] if os_lines else "unknown OS"
            except WorkflowError:
                osname = "unknown OS"

            sudo_note = "logged in as root (sudo not needed)" if self.remote.is_root else "working"
            return f"Connection OK.\nServer: {osname}\napt-offline: {version}\nsudo: {sudo_note}"
        finally:
            self.remote.close()


    def run(self):
        cfg = self.cfg
        rounds = (["update"] if cfg.refresh_lists else []) + ["upgrade"]
        summary = []

        try:
            self.remote.connect()
            self._preflight()
            self.rsig = self.remote.rpath(cfg.remote_sig_dir)
            self.rpkg = self.remote.rpath(cfg.remote_pkg_dir)
            self.remote.makedirs(self.rsig, self.created_remote)
            self.remote.makedirs(self.rpkg, self.created_remote)
            self._mk_local(cfg.local_sig_dir)
            self._mk_local(cfg.local_pkg_dir)

            finished = []
            for kind in rounds:
                self._check_cancel()
                summary.append(self._round(kind))
                finished.append(kind)

            if cfg.cleanup:
                self._cleanup(finished)
            else:
                self.ui.log("\nCleanup disabled - sig and package files were left in place.")

            reboot = self._reboot_check()

            if reboot:
                summary.append(reboot)

            self.ui.progress(1.0)
            self.ui.status("Finished.")

            return "\n".join(summary)
        finally:
            self.remote.close()


    def _mk_local(self, path):
        cur, missing = os.path.abspath(path), []
        while cur and not os.path.exists(cur):
            missing.append(cur)
            parent = os.path.dirname(cur)

            if parent == cur:
                break
            cur = parent

        os.makedirs(path, exist_ok=True)
        self.created_local.extend(reversed(missing))


    def _round(self, kind):
        cfg, remote = self.cfg, self.remote
        sig_name = f"apt-offline-{kind}.sig"
        rsig = posixpath.join(self.rsig, sig_name)
        lsig = os.path.join(cfg.local_sig_dir, sig_name)
        rdir = posixpath.join(self.rpkg, kind)
        ldir = os.path.join(cfg.local_pkg_dir, kind)
        self.state[kind] = {"rsig": rsig, "lsig": lsig, "rdir": rdir, "ldir": ldir,
                            "uploaded": [], "downloaded": []}
        
        state = self.state[kind]

        self._stage(kind, "Generating request on the server")
        remote.run(["rm", "-f", "--", rsig], sudo=True, show=False)
        set_cmd = ["apt-offline", "set", rsig, f"--{kind}"]

        if kind == "upgrade" and cfg.upgrade_type != "upgrade":
            set_cmd += ["--upgrade-type", cfg.upgrade_type]

        code, lines = remote.run(set_cmd, sudo=True, check=False)
        if code != 0:
            text = "\n".join(lines).lower()
            if "0 bytes" in text or "no payload" in text:
                self.ui.log("Nothing to download - the server is already up to date.")

                return f"{kind}: nothing to do (already up to date)"
            raise WorkflowError(f"'apt-offline set' failed (exit {code}):\n" + "\n".join(lines[-10:]))
        
        remote.run(["chmod", "a+r", "--", rsig], sudo=True, check=False, show=False)

        if not remote.exists(rsig):
            self.ui.log("The server did not create a signature file - nothing to do.")
            return f"{kind}: nothing to do"

        self._check_cancel()
        self._stage(kind, "Fetching signature file (SFTP)")
        remote.sftp.get(rsig, lsig)
        self.ui.log(f"Saved {lsig}")

        try:
            entries = parse_sig(lsig)
        except EmptySig:
            self.ui.log("The signature file is empty: nothing to download or install.")
            return f"{kind}: nothing to do" + (" (system is up to date)" if kind == "upgrade" else "")
        except ValueError as exc:
            raise WorkflowError(f"Bad signature file: {exc}")
        
        self.ui.log(f"{len(entries)} files listed.")

        good = self._download(kind, entries, ldir, tolerant=(kind == "update"))
        state["downloaded"] = good
        self._upload(kind, good, ldir, rdir, state)

        self._check_cancel()

        if kind == "upgrade" and cfg.confirm_before_install:
            if not self.ui.ask("install", self._install_summary(good)):
                raise Cancelled()
            
        self._stage(kind, "Installing on the server")
        remote.run(["apt-offline", "install", rdir], sudo=True)
        result = f"{kind}: {len(good)} files transferred and installed"

        if kind == "upgrade":
            self._stage(kind, f"Running apt-get {cfg.upgrade_type}")
            remote.run(["apt-get", "-y", "--no-download",
                        "-o", "Dpkg::Options::=--force-confdef",
                        "-o", "Dpkg::Options::=--force-confold",
                        cfg.upgrade_type],
                       sudo=True, env=APT_ENV)
            result = f"upgrade: {len(good)} packages transferred, apt-get {cfg.upgrade_type} completed"
        return result


    def _download(self, kind, entries, ldir, tolerant):
        self._stage(kind, "Downloading from mirrors", 0.0)
        total = sum(entry.size for entry in entries)
        sizes_known = all(entry.size for entry in entries)
        counters = {"bytes": 0, "files": 0}
        lock, throttle = threading.Lock(), Throttle()


        def report(force=False):
            if force or throttle.ready():
                if sizes_known and total:
                    frac = counters["bytes"] / total
                    detail = f"{human(max(counters['bytes'], 0))} / {human(total)}"
                else:
                    frac = counters["files"] / len(entries)
                    detail = ""

                self.ui.progress(max(0.0, min(1.0, frac)))
                self.ui.status(f"{self.stage}: {counters['files']}/{len(entries)} files  {detail}")


        def on_bytes(delta):
            with lock:
                counters["bytes"] += delta
            report()


        def on_done(_entry, _status):
            with lock:
                counters["files"] += 1
            report()

        downloader = Downloader(entries, ldir, self.ui.log, on_bytes, on_done, self.cancel)
        counts, failed, good = downloader.run()

        report(force=True)
        self.ui.log(f"Download: {counts['ok']} new, {counts['skipped']} already present, "
                    f"{counts['failed']} failed.")
        if failed:
            if not tolerant:
                raise WorkflowError(
                    f"{len(failed)} package(s) could not be downloaded (possibly superseded on the "
                    "mirror). Nothing was installed; run again to get a fresh request.\n"
                    + "\n".join(failed[:10]))
            
            self.ui.log("Some package-list variants were unavailable. This is normal "
                        "(apt-offline lists alternative compressions); continuing.")
            
        if not good:
            raise WorkflowError("Nothing could be downloaded - does this PC have internet access?")
        
        return good

    def _upload(self, kind, good, ldir, rdir, state):
        self._stage(kind, "Uploading to the server (SFTP)", 0.0)
        self.remote.makedirs(rdir, self.created_remote)

        # Calculate the total size of all files to be uploaded for progress tracking.
        sizes = {entry.filename: os.path.getsize(os.path.join(ldir, entry.filename)) for entry in good}
        total = sum(sizes.values()) or 1
        sent, throttle = 0, Throttle()

        for index, entry in enumerate(good, 1):
            self._check_cancel()
            local = os.path.join(ldir, entry.filename)
            remote_path = posixpath.join(rdir, entry.filename)
            size = sizes[entry.filename]

            try:
                if self.remote.sftp.stat(remote_path).st_size == size:
                    sent += size
                    state["uploaded"].append(entry)
                    continue

            except OSError:
                pass

            def callback(done, _total, base=sent, i=index):
                if self.cancel.is_set():
                    raise Cancelled()
                
                if throttle.ready():
                    self.ui.progress((base + done) / total)
                    self.ui.status(f"{self.stage}: file {i}/{len(good)}  "
                                   f"{human(base + done)} / {human(total)}")

            self.remote.put(local, remote_path, callback)
            sent += size
            state["uploaded"].append(entry)

        self.ui.progress(1.0)
        self.ui.log(f"Uploaded {len(good)} files to {rdir}")

    @staticmethod
    def _install_summary(good):
        names = []
        for entry in good:
            base = entry.filename[:-4] if entry.filename.endswith(".deb") else entry.filename
            base = base.replace("%3a", ":").replace("%3A", ":")
            bits = base.split("_")
            names.append(f"{bits[0]}  {bits[1]}" if len(bits) >= 2 else base)

        total = sum(entry.size for entry in good)
        shown = "\n".join(names[:25]) + (f"\n... and {len(names) - 25} more" if len(names) > 25 else "")

        return f"{len(good)} packages ({human(total)}) will be installed on the server:\n\n{shown}"

    def _cleanup(self, finished):
        cfg = self.cfg
        self._stage("", "Cleaning up")

        if cfg.cleanup_remote:
            for kind in finished:
                state = self.state.get(kind)
                if not state:
                    continue
                self._sweep_remote(state["rdir"])
                self.remote.run(["rm", "-f", "--", state["rsig"]], sudo=True, check=False, show=False)

                try:
                    self.remote.sftp.rmdir(state["rdir"])
                except OSError:
                    pass

            for directory in reversed(self.created_remote):
                try:
                    self.remote.sftp.rmdir(directory)
                except OSError:
                    pass

            self.ui.log("Server: removed uploaded packages and signature files.")

            if cfg.apt_clean:
                self.remote.run(["apt-get", "clean"], sudo=True, check=False, env=APT_ENV)


        if cfg.cleanup_local:
            for kind in finished:
                state = self.state.get(kind)

                if not state:
                    continue
                self._sweep_local(state["ldir"])
                self._rm_local(state["lsig"])

                try:
                    os.rmdir(state["ldir"])
                except OSError:
                    pass

            for directory in reversed(self.created_local):
                try:
                    os.rmdir(directory)
                except OSError:
                    pass

            self.ui.log("PC: removed downloaded packages and signature files.")


    def _sweep_remote(self, directory):
        try:
            for attr in self.remote.sftp.listdir_attr(directory):
                if not stat.S_ISDIR(attr.st_mode or 0):
                    try:
                        self.remote.sftp.remove(posixpath.join(directory, attr.filename))
                    except OSError:
                        pass
        except OSError:
            pass


    def _sweep_local(self, directory):
        try:
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    self._rm_local(path)
        except OSError:
            pass


    @staticmethod
    def _rm_local(path):
        try:
            os.remove(path)
        except OSError:
            pass


    def _reboot_check(self):
        code, _ = self.remote.run(["test", "-f", "/var/run/reboot-required"],
                                  check=False, show=False)
        if code != 0:
            return None
        packages = ""
        second_code, lines = self.remote.run(["cat", "/var/run/reboot-required.pkgs"],
                                             check=False, show=False)
        if second_code == 0 and lines:
            packages = " (" + ", ".join(sorted(set(lines))[:8]) + ")"

        message = f"A reboot of the server is required{packages}."
        self.ui.log("\n*** " + message + " ***")
        
        return message