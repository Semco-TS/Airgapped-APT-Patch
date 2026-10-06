import base64
import hashlib
import os
import posixpath
import re
import shlex
import socket

from .errors import Cancelled, WorkflowError


KNOWN_HOSTS_FILE = os.path.join(os.path.expanduser("~"), ".apt_offline_updater_known_hosts")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
NOISE_RE = re.compile(r"^\(Reading database \.\.\. \d+%$")
APT_ENV = {
	"DEBIAN_FRONTEND": "noninteractive",
	"APT_LISTCHANGES_FRONTEND": "none",
	"NEEDRESTART_MODE": "l",
}

try:
	import paramiko
except ImportError:
	paramiko = None


def human(n):
	n = float(n)
	for unit in ("B", "KiB", "MiB", "GiB"):
		if n < 1024 or unit == "GiB":
			return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
		n /= 1024


class Remote:
	
	def __init__(self, cfg, log, cancel, ask):
		self.cfg, self.log, self.cancel, self.ask = cfg, log, cancel, ask
		self.client = None
		self.sftp = None
		self.home = "/"
		self.is_root = cfg.username == "root"
		self.sudo_pw = cfg.sudo_password or cfg.password

	def connect(self):
		if paramiko is None:
			raise WorkflowError("The 'paramiko' package is not installed (pip install paramiko).")
		
		remote = self
		client = paramiko.SSHClient()
		
		if os.path.exists(KNOWN_HOSTS_FILE):
			try:
				client.load_host_keys(KNOWN_HOSTS_FILE)
			except (OSError, paramiko.SSHException):
				pass

		class AskPolicy(paramiko.MissingHostKeyPolicy):
			def missing_host_key(self, cl, hostname, key):
				fp = "SHA256:" + base64.b64encode(
					hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
				
				ok = remote.ask("hostkey", f"{hostname}\n{key.get_name()} {fp}")
				if not ok:
					raise WorkflowError("Server host key was not trusted - connection aborted.")
				
				cl.get_host_keys().add(hostname, key.get_name(), key)
				
				try:
					cl.save_host_keys(KNOWN_HOSTS_FILE)
				except OSError:
					remote.log("Warning: could not save host key to " + KNOWN_HOSTS_FILE)

		client.set_missing_host_key_policy(AskPolicy())
		cfg = self.cfg
		kwargs = dict(hostname=cfg.host, port=cfg.port, username=cfg.username,
					  timeout=15, banner_timeout=30, auth_timeout=30,
					  allow_agent=False, look_for_keys=False)
		
		if cfg.key_file:
			kwargs["key_filename"] = cfg.key_file
			kwargs["passphrase"] = cfg.password or None
		else:
			kwargs["password"] = cfg.password
			
		self.log(f"Connecting to {cfg.username}@{cfg.host}:{cfg.port} ...")
		
		try:
			client.connect(**kwargs)
			
		except paramiko.BadHostKeyException:
			raise WorkflowError(
				"HOST KEY MISMATCH! The server's key differs from the one saved earlier. "
				f"If the server was legitimately reinstalled, delete its line from {KNOWN_HOSTS_FILE}.")
		
		except paramiko.AuthenticationException:
			raise WorkflowError("SSH authentication failed (check username / password / key).")
		
		except (paramiko.SSHException, OSError, socket.timeout) as exc:
			raise WorkflowError(f"Cannot connect: {exc}")

        
		client.get_transport().set_keepalive(30)
		
		self.client = client
		self.sftp = client.open_sftp()
		self.home = self.sftp.normalize(".")
		self.log("Connected.")
		

	def close(self):
		for obj in (self.sftp, self.client):
			try:
				if obj:
					obj.close()
			except Exception:
				pass
			

	def run(self, argv, sudo=False, env=None, check=True, show=True, label=None):
		parts = list(argv)
		if env:
			parts = ["env"] + [f"{key}={value}" for key, value in env.items()] + parts
			
		quoted = " ".join(shlex.quote(part) for part in parts)
		use_sudo = sudo and not self.is_root
		
		if use_sudo:
			cmd = "sudo -S -p '' -- sh -c 'exec \"$@\" </dev/null' sh " + quoted
		else:
			cmd = quoted
		if show:
			self.log("$ " + ("sudo " if use_sudo else "") + (label or " ".join(argv)))
			
		chan = self.client.get_transport().open_session()
		chan.set_combine_stderr(True)
		chan.exec_command(cmd)
		
		if use_sudo:
			chan.sendall((self.sudo_pw + "\n").encode("utf-8"))
			
		chan.shutdown_write()
		chan.settimeout(0.3)

		lines, buf = [], ""

		def emit(text):
			text = ANSI_RE.sub("", text).rstrip()
			if text and not NOISE_RE.match(text):
				lines.append(text)
				if show:
					self.log("    " + text)

		while True:
			if self.cancel.is_set():
				chan.close()
				raise Cancelled()
			
			try:
				data = chan.recv(65536)
			except socket.timeout:
				continue
			if not data:
				break
			
			buf += data.decode("utf-8", "replace")
			pieces = re.split(r"[\r\n]+", buf)
			buf = pieces.pop()
			
			for piece in pieces:
				emit(piece)
				
		emit(buf)
		code = chan.recv_exit_status()
		chan.close()
		
		if check and code != 0:
			tail = "\n".join(lines[-12:])
			raise WorkflowError(f"Remote command failed (exit {code}): {' '.join(argv)}\n{tail}")


		return code, lines




	def rpath(self, path):
		path = path.strip()
		
		if path == "~":
			path = self.home
			
		elif path.startswith("~/"):
			path = posixpath.join(self.home, path[2:])
			
		elif not path.startswith("/"):
			path = posixpath.join(self.home, path)
			
		return posixpath.normpath(path)

    

	def makedirs(self, path, created):
		missing, cur = [], path
		
		while cur not in ("/", ""):
			try:
				self.sftp.stat(cur)
				break
			except OSError:
				missing.append(cur)
				cur = posixpath.dirname(cur)
				
		for directory in reversed(missing):
			try:
				self.sftp.mkdir(directory)
			except OSError as exc:
				raise WorkflowError(f"Cannot create remote directory {directory}: {exc}")
			
			created.append(directory)



	def exists(self, path):
		try:
			self.sftp.stat(path)
			return True
		except OSError:
			return False

        

	def put(self, local, remote, cb):
		tmp = remote + ".part"
		
		try:
			self.sftp.put(local, tmp, callback=cb, confirm=True)
		except BaseException:
			try:
				self.sftp.remove(tmp)
			except OSError:
				pass
			raise
        
		try:
			self.sftp.posix_rename(tmp, remote)
		except (IOError, OSError):
			try:
				self.sftp.remove(remote)
			except OSError:
				pass
			
			self.sftp.rename(tmp, remote)