import hashlib
import os
import shlex
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .errors import Cancelled, EmptySig


USER_AGENT = "apt-offline-ssh-updater/1.0"
WORKERS = 4
RETRIES = 3
HTTP_TIMEOUT = 30
CHUNK = 64 * 1024
HASHES = {"md5sum": "md5", "md5": "md5", "sha1": "sha1", "sha256": "sha256", "sha512": "sha512"}
PERMANENT_HTTP = (400, 401, 403, 404, 410)


@dataclass
class Entry:
    url: str
    filename: str
    size: int
    algo: str
    digest: str


def parse_sig(path):
    entries, seen = [], set()

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()

            if not line or line.startswith("#"):
                continue

            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise ValueError(f"Line {lineno}: cannot parse ({exc})")
            
            if len(parts) < 2:
                raise ValueError(f"Line {lineno}: expected 'URL' FILENAME SIZE CHECKSUM")
            
            url, filename = parts[0], parts[1]
            size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            algo, digest = "", ""

            if len(parts) > 3 and ":" in parts[3]:
                name, digest = parts[3].split(":", 1)
                algo = HASHES.get(name.lower(), "")
                digest = digest.lower() if algo else ""

            if not url.lower().startswith(("http://", "https://", "ftp://")):
                raise ValueError(f"Line {lineno}: unsupported URL scheme: {url}")
            
            if (filename != os.path.basename(filename) or filename in ("", ".", "..")
                    or "\\" in filename or ":" in filename or "/" in filename):
                raise ValueError(f"Line {lineno}: unsafe filename: {filename!r}")
            
            if filename in seen:
                continue

            seen.add(filename)
            entries.append(Entry(url, filename, size, algo, digest))

    if not entries:
        raise EmptySig("No downloadable entries found in the signature file.")

    
    return entries


def file_digest(path, algo):
    digest = hashlib.new(algo)

    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def is_valid(path, entry):
    if not os.path.isfile(path):
        return False
    if entry.size and os.path.getsize(path) != entry.size:
        return False
    if entry.algo and file_digest(path, entry.algo) != entry.digest:
        return False
    
    return True


class Downloader:
    def __init__(self, entries, out_dir, on_log, on_bytes, on_file_done, cancel_event):
        self.entries, self.out_dir = entries, out_dir
        self.on_log, self.on_bytes, self.on_file_done = on_log, on_bytes, on_file_done
        self.cancel = cancel_event

    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _one(self, entry):
        dest = os.path.join(self.out_dir, entry.filename)

        if is_valid(dest, entry):
            self.on_bytes(os.path.getsize(dest))
            self.on_log(f"SKIP  {entry.filename} (already downloaded, verified)")
            return "skipped"
        
        part = dest + ".part"
        last_err = None

        for attempt in range(1, RETRIES + 1):
            if self.cancel.is_set():
                raise Cancelled()

            
            counted = 0
            try:
                req = urllib.request.Request(entry.url, headers={"User-Agent": USER_AGENT})

                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(part, "wb") as out:
                    while True:
                        if self.cancel.is_set():
                            raise Cancelled()
                        
                        block = resp.read(CHUNK)
                        if not block:
                            break

                        out.write(block)
                        counted += len(block)
                        self.on_bytes(len(block))


                if entry.size and os.path.getsize(part) != entry.size:
                    raise IOError(f"size mismatch: got {os.path.getsize(part)}, expected {entry.size}")
                
                if entry.algo:
                    got = file_digest(part, entry.algo)

                    if got != entry.digest:
                        raise IOError(f"{entry.algo} mismatch: got {got}, expected {entry.digest}")
                    
                os.replace(part, dest)
                self.on_log(f"OK    {entry.filename}")

                return "ok"
            except Cancelled:
                self._rm(part)
                raise
            except (urllib.error.URLError, OSError, IOError, TimeoutError) as exc:
                last_err = exc
                self.on_bytes(-counted)
                self._rm(part)

                if isinstance(exc, urllib.error.HTTPError) and exc.code in PERMANENT_HTTP:
                    break

                self.on_log(f"RETRY {entry.filename} (attempt {attempt}/{RETRIES}): {exc}")
                time.sleep(min(2 * attempt, 5))
                
        self.on_log(f"FAIL  {entry.filename}: {last_err}")
        return "failed"

    def run(self):
        """Returns (counts, failed_filenames, good_entries)."""
        os.makedirs(self.out_dir, exist_ok=True)
        counts = {"ok": 0, "skipped": 0, "failed": 0}
        failed, good = [], []

        def task(entry):
            if self.cancel.is_set():
                raise Cancelled()
            
            status = self._one(entry)
            self.on_file_done(entry, status)

            return entry, status

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(task, entry) for entry in self.entries]

            try:
                for future in futures:
                    entry, status = future.result()
                    counts[status] += 1
                    if status == "failed":
                        failed.append(entry.filename)
                    else:
                        good.append(entry)

            except Cancelled:
                for future in futures:
                    future.cancel()

                raise

            
        return counts, failed, good