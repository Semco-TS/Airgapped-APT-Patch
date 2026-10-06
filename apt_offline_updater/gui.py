import os
import sys
import threading
import traceback

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import Config, load_settings, save_settings
from .errors import Cancelled, WorkflowError
from .remote import paramiko
from .workflow import Workflow


class WorkflowUi(QObject):
    log_message = Signal(str)
    status_message = Signal(str)
    progress_value = Signal(object)
    ask_requested = Signal(str, str, object)

    def __init__(self, cancel):
        super().__init__()
        self.cancel = cancel

    def log(self, message):
        self.log_message.emit(message)

    def status(self, message):
        self.status_message.emit(message)

    def progress(self, fraction):
        self.progress_value.emit(fraction)

    def ask(self, kind, text):
        box = {"event": threading.Event(), "answer": False}
        self.ask_requested.emit(kind, text, box)
        while not box["event"].wait(0.2):
            if self.cancel.is_set():
                raise Cancelled()
        return box["answer"]


class WorkflowThread(QThread):
    result_ready = Signal(str, str, str)
    traceback_ready = Signal(str)

    def __init__(self, cfg, ui, cancel, mode):
        super().__init__()
        self.cfg, self.ui, self.cancel, self.mode = cfg, ui, cancel, mode

    def run(self):
        workflow = Workflow(self.cfg, self.ui, self.cancel)
        try:
            message = workflow.test() if self.mode == "test" else workflow.run()
            self.result_ready.emit("ok", message, self.mode)
        except Cancelled:
            self.result_ready.emit("cancelled", "", self.mode)
        except WorkflowError as exc:
            self.result_ready.emit("error", str(exc), self.mode)
        except Exception as exc:
            self.traceback_ready.emit(traceback.format_exc())
            self.result_ready.emit("error", f"Unexpected error: {exc!r}", self.mode)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Airgapped APT Patch v{__version__}")
        self.resize(880, 860)
        self.setMinimumSize(760, 700)
        self.cfg0 = load_settings()
        self.fields = {}
        self.controls = []
        self.subcontrols = []
        self.cancel = None
        self.worker = None
        self.ui = None
        self._build_ui()

    def _build_ui(self):
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        self.setCentralWidget(central)

        connection = QGroupBox("Server connection (SSH)")
        connection_form = QFormLayout(connection)
        host_port = QHBoxLayout()
        self._add_entry(host_port, "host", self.cfg0.host)
        host_port.addWidget(QLabel("Port:"))
        self.port = QLineEdit(str(self.cfg0.port))
        self.port.setMaximumWidth(90)
        host_port.addWidget(self.port)
        host_port.addStretch(1)
        self.controls.append(self.port)
        connection_form.addRow("Host / IP:", host_port)
        connection_form.addRow("Username:", self._entry("username", self.cfg0.username))
        password = self._entry("password", "")
        password.setEchoMode(QLineEdit.EchoMode.Password)
        connection_form.addRow("SSH password (or key passphrase):", password)
        connection_form.addRow("Private key file (optional):", self._path_field("key_file", self.cfg0.key_file, file=True))
        sudo_password = self._entry("sudo_password", "")
        sudo_password.setEchoMode(QLineEdit.EchoMode.Password)
        connection_form.addRow("Sudo password (blank = same as SSH):", sudo_password)
        outer.addWidget(connection)

        directories = QGroupBox("Working directories")
        directory_form = QFormLayout(directories)
        directory_form.addRow("Server: .sig file folder:", self._entry("remote_sig_dir", self.cfg0.remote_sig_dir))
        directory_form.addRow("Server: package upload folder:", self._entry("remote_pkg_dir", self.cfg0.remote_pkg_dir))
        directory_form.addRow("This PC: .sig file folder:", self._path_field("local_sig_dir", self.cfg0.local_sig_dir))
        directory_form.addRow("This PC: package download folder:", self._path_field("local_pkg_dir", self.cfg0.local_pkg_dir))
        outer.addWidget(directories)

        options = QGroupBox("Options")
        options_layout = QVBoxLayout(options)
        self.refresh_lists = self._checkbox(
            "Refresh package lists first (apt-offline --update, like 'apt-get update')",
            self.cfg0.refresh_lists)
        options_layout.addWidget(self.refresh_lists)
        upgrade_row = QHBoxLayout()
        upgrade_row.addWidget(QLabel("Upgrade type:"))
        self.upgrade_type = QComboBox()
        self.upgrade_type.addItems(("upgrade", "dist-upgrade"))
        self.upgrade_type.setCurrentText(self.cfg0.upgrade_type)
        self.controls.append(self.upgrade_type)
        upgrade_row.addWidget(self.upgrade_type)
        upgrade_row.addStretch(1)
        options_layout.addLayout(upgrade_row)
        self.confirm_before_install = self._checkbox(
            "Ask for confirmation (showing the package list) before installing",
            self.cfg0.confirm_before_install)
        options_layout.addWidget(self.confirm_before_install)
        self.cleanup = self._checkbox("Clean up afterwards (only after a successful install)", self.cfg0.cleanup)
        options_layout.addWidget(self.cleanup)
        self.cleanup_remote = self._checkbox("Server: delete uploaded packages and .sig files", self.cfg0.cleanup_remote)
        self.cleanup_local = self._checkbox("This PC: delete downloaded packages and .sig files", self.cfg0.cleanup_local)
        self.apt_clean = self._checkbox(
            "Server: also run 'apt-get clean' (empties /var/cache/apt/archives)", self.cfg0.apt_clean)
        for checkbox in (self.cleanup_remote, self.cleanup_local, self.apt_clean):
            options_layout.addWidget(checkbox)
            self.subcontrols.append(checkbox)
        self._toggle_cleanup_options(self.cleanup.isChecked())
        self.cleanup.toggled.connect(self._toggle_cleanup_options)
        outer.addWidget(options)

        action_row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.run_button = QPushButton("Run update")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        action_row.addWidget(self.test_button)
        action_row.addWidget(self.run_button)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.progress, 1)
        outer.addLayout(action_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        outer.addWidget(self.log, 1)
        self.status = QLabel("Idle.")
        outer.addWidget(self.status)

        self.test_button.clicked.connect(lambda: self._start("test"))
        self.run_button.clicked.connect(lambda: self._start("run"))
        self.cancel_button.clicked.connect(self._cancel)

    def _add_entry(self, layout, key, value):
        edit = self._entry(key, value)
        layout.addWidget(edit)
        return edit

    def _entry(self, key, value):
        edit = QLineEdit(value)
        self.fields[key] = edit
        self.controls.append(edit)
        return edit

    def _path_field(self, key, value, file=False):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        edit = self._entry(key, value)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse...")
        button.clicked.connect(lambda: self._browse(key, file))
        layout.addWidget(button)
        self.controls.append(button)
        return row

    def _checkbox(self, text, checked):
        checkbox = QCheckBox(text)
        checkbox.setChecked(checked)
        self.controls.append(checkbox)
        return checkbox

    def _browse(self, key, file):
        current = self.fields[key].text()
        if file:
            path, _ = QFileDialog.getOpenFileName(self, "Select private key", current)
        else:
            dialog = QFileDialog(self, "Select folder", current)
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            path = dialog.selectedFiles()[0] if dialog.exec() == QDialog.DialogCode.Accepted else ""
        if path:
            self.fields[key].setText(os.path.normpath(path) if not file else path)

    def _toggle_cleanup_options(self, enabled):
        for control in self.subcontrols:
            control.setEnabled(enabled)

    def _collect(self):
        try:
            port = int(self.port.text())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Invalid input", "Port must be a number between 1 and 65535.")
            return None
        
        cfg = Config(
            host=self.fields["host"].text().strip(), port=port,
            username=self.fields["username"].text().strip(),
            password=self.fields["password"].text(), key_file=self.fields["key_file"].text().strip(),
            sudo_password=self.fields["sudo_password"].text(),
            remote_sig_dir=self.fields["remote_sig_dir"].text().strip(),
            remote_pkg_dir=self.fields["remote_pkg_dir"].text().strip(),
            local_sig_dir=self.fields["local_sig_dir"].text().strip(),
            local_pkg_dir=self.fields["local_pkg_dir"].text().strip(),
            refresh_lists=self.refresh_lists.isChecked(), upgrade_type=self.upgrade_type.currentText(),
            confirm_before_install=self.confirm_before_install.isChecked(), cleanup=self.cleanup.isChecked(),
            cleanup_remote=self.cleanup_remote.isChecked(), cleanup_local=self.cleanup_local.isChecked(),
            apt_clean=self.apt_clean.isChecked())
        
        if not cfg.host or not cfg.username:
            QMessageBox.warning(self, "Missing input", "Host and username are required.")
            return None
        
        if not all((cfg.remote_sig_dir, cfg.remote_pkg_dir, cfg.local_sig_dir, cfg.local_pkg_dir)):
            QMessageBox.warning(self, "Missing input", "All four working directories must be set.")
            return None
        return cfg

    def _set_running(self, running):
        for control in self.controls:
            control.setEnabled(not running)
        self.test_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

        if not running:
            self.upgrade_type.setEnabled(True)
            self._toggle_cleanup_options(self.cleanup.isChecked())

    def _start(self, mode):
        cfg = self._collect()
        if cfg is None:
            return
        save_settings(cfg)

        if mode == "run" and QMessageBox.question(
                self, "Start update",
                f"Run the update workflow on {cfg.username}@{cfg.host}:{cfg.port}?\n\n"
                "This installs package updates on the server.") != QMessageBox.StandardButton.Yes:
            return
        
        self.cancel = threading.Event()
        self.log.clear()
        self._set_running(True)
        self.ui = WorkflowUi(self.cancel)
        self.ui.log_message.connect(self._add_log)
        self.ui.status_message.connect(self.status.setText)
        self.ui.progress_value.connect(self._set_progress)
        self.ui.ask_requested.connect(self._handle_ask)
        self.worker = WorkflowThread(cfg, self.ui, self.cancel, mode)
        self.worker.traceback_ready.connect(self._add_log)
        self.worker.result_ready.connect(self._handle_result)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _cancel(self):
        if self.cancel:
            self.cancel.set()
            self.status.setText("Cancelling...")

    def _add_log(self, message):
        self.log.appendPlainText(message)

    def _set_progress(self, fraction):
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(int(fraction * 100))

    def _handle_ask(self, kind, text, box):
        if kind == "hostkey":
            host, fingerprint = text.split("\n", 1)
            box["answer"] = QMessageBox.question(
                self, "Unknown server",
                f"The authenticity of host '{host}' can't be established.\n\n"
                f"Key fingerprint:\n{fingerprint}\n\n"
                "Only continue if this matches the server's real fingerprint "
                "(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub on the server).\n\n"
                "Trust this server and remember its key?") == QMessageBox.StandardButton.Yes
            
        elif kind == "install":
            box["answer"] = QMessageBox.question(
                self, "Confirm installation", text + "\n\nProceed with the installation?") \
                == QMessageBox.StandardButton.Yes
            
        box["event"].set()


    def _handle_result(self, outcome, message, mode):
        self.progress.setRange(0, 100)
        self._set_running(False)

        if outcome == "ok":
            self.status.setText("Connection OK." if mode == "test" else "Finished successfully.")
            self._add_log("\n" + message)
            QMessageBox.information(self, "Done" if mode == "run" else "Connection test", message)

        elif outcome == "cancelled":
            self.status.setText("Cancelled. Files were left in place; run again to resume.")
            self._add_log("\nCancelled.")

        else:
            self.status.setText("Failed.")
            self._add_log("\nERROR: " + message)
            QMessageBox.critical(self, "Error", message)


    def _worker_finished(self):
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        self.ui = None


def run_gui():
    app = QApplication(sys.argv)
    if paramiko is None:
        QMessageBox.critical(
            None, "Missing dependency",
            "This program needs the 'paramiko' package.\n\n"
            "Open a Command Prompt and run:\n    pip install paramiko\n\nthen start it again.")
        return
    
    window = MainWindow()
    window.show()
    app.exec()