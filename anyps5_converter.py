#!/usr/bin/env python3
"""AnyPS5 Converter - automatic conversion of a PS5 game into a native program with AnyPS5.

Point it at the game folder (containing eboot.bin) or at eboot.bin itself. The tool:
  1. unwraps SELF/fSELF containers into clean ELF files (eboot, sce_module, plugins, modules),
  2. downloads the AnyPS5 relinker and system libraries from the GitHub release (cached),
  3. runs the relinker for the selected OS and CPU,
  4. assembles the output folder: program, libs/ and app0/ (game files linked or copied).

Without arguments it opens a window; with arguments it runs on the command line (--help).
"""

import argparse
import json
import os
import platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))
import unwrap_self  # noqa: E402

REPO = "boykopovar/AnyPS5"
VERSION = "1.0"
SELF_MAGICS = (b"\x4f\x15\x3d\x1d", b"\x54\x14\xf5\xee")
ELF_MAGIC = b"\x7fELF"
MODULE_EXTS = {".prx", ".sprx"}
SKIP_DIRS = {"sce_sys", "fakelib"}
AUTO_MODULE_DIRS = {"sce_module", "sce_modules", "prx"}
MARKER = ".anyps5-converter.json"
STAGE = ".anyps5-stage"
OS_CHOICES = ("windows", "linux", "macos")
CPU_CHOICES = ("auto", "amd", "intel", "rosetta")
LINK_CHOICES = ("auto", "link", "copy")
IS_WINDOWS = sys.platform.startswith("win")


class ConvertError(Exception):
    pass


@dataclass
class Config:
    game: str
    output: str = ""
    target_os: str = ""
    cpu: str = "auto"
    version: str = "latest"
    link_mode: str = "auto"
    run_after: bool = False
    windows_gui: bool = False
    include_executables: bool = False
    relinker: str = ""
    libs: str = ""
    executables: list = field(default_factory=list)
    extra_args: list = field(default_factory=list)
    force: bool = False
    keep_stage: bool = False
    verbose: bool = False


# ---------------------------------------------------------------- host detection

def host_os():
    if IS_WINDOWS:
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _run_quiet(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cpu_info():
    """Return (CPU name, vendor 'amd'/'intel'/'arm'/'?')."""
    name, vendor = platform.processor() or "", ""
    machine = platform.machine().lower()
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
                vendor = winreg.QueryValueEx(key, "VendorIdentifier")[0]
        except OSError:
            vendor = os.environ.get("PROCESSOR_IDENTIFIER", "")
    elif sys.platform == "darwin":
        name = _run_quiet(["sysctl", "-n", "machdep.cpu.brand_string"]) or name
        vendor = _run_quiet(["sysctl", "-n", "machdep.cpu.vendor"])
        if _run_quiet(["sysctl", "-n", "sysctl.proc_translated"]) == "1" or "apple" in name.lower():
            vendor = "arm"
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.startswith("vendor_id") and not vendor:
                    vendor = line.split(":", 1)[1].strip()
                elif line.startswith("model name") and not name:
                    name = line.split(":", 1)[1].strip()
        except OSError:
            pass
    if machine in ("arm64", "aarch64") or vendor == "arm":
        return name or machine, "arm"
    if "amd" in vendor.lower():
        return name, "amd"
    if "intel" in vendor.lower():
        return name, "intel"
    return name or machine, "?"


def resolve_cpu(cpu, target_os):
    if cpu != "auto":
        return cpu
    _, vendor = cpu_info()
    if vendor == "amd":
        return "amd"
    if vendor == "arm":
        if host_os() == "macos" and target_os == "macos":
            return "rosetta"
        raise ConvertError("ARM CPU: AnyPS5 needs x86-64 (on Apple Silicon choose target macOS and CPU 'rosetta').")
    return "intel"


# ---------------------------------------------------------------- file helpers

def magic_of(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return None
    if head == ELF_MAGIC:
        return "elf"
    if head in SELF_MAGICS:
        return "self"
    return None


def is_link(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def safe_remove(path):
    """Remove a path without following junctions/symlinks (removes the link only, never its target)."""
    path = Path(path)
    if not os.path.lexists(path):
        return
    if is_link(path):
        st = os.lstat(path)
        if getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0):
            os.rmdir(path)
        else:
            os.unlink(path)
    elif path.is_dir():
        for entry in os.scandir(path):
            safe_remove(entry.path)
        os.rmdir(path)
    else:
        try:
            os.unlink(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(path)


def is_inside(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


# ---------------------------------------------------------------- converter

class Converter:
    def __init__(self, config, log=print, progress=None):
        self.c = config
        self.log = log
        self.progress = progress or (lambda text: None)
        self.copied_fallback = False

    # ---- game analysis

    def locate_game(self):
        source = Path(self.c.game).expanduser()
        if not source.exists():
            raise ConvertError(f"Does not exist: {source}")
        if source.is_file():
            eboot, root = source, source.parent
        else:
            root, eboot = source, source / "eboot.bin"
            if not eboot.is_file():
                raise ConvertError(f"No eboot.bin in {root}. Select the game folder or the eboot.bin file.")
        if magic_of(eboot) is None:
            raise ConvertError(f"{eboot.name} is neither an ELF nor a SELF file (wrong file or an encrypted PKG).")
        return eboot.resolve(), root.resolve()

    def read_title(self, root):
        param = root / "sce_sys" / "param.json"
        if not param.is_file():
            self.log("WARNING: sce_sys/param.json is missing - some games need it at startup.")
            return None, None
        try:
            data = json.loads(param.read_text(encoding="utf-8-sig"))
            title_id = data.get("titleId")
            localized = data.get("localizedParameters", {})
            lang = localized.get("defaultLanguage", "en-US")
            name = (localized.get(lang) or {}).get("titleName")
            if not name:
                name = next((v.get("titleName") for v in localized.values() if isinstance(v, dict) and v.get("titleName")), None)
            return title_id, name
        except (ValueError, AttributeError) as error:
            self.log(f"WARNING: cannot read param.json: {error}")
            return None, None

    def scan(self, root, eboot):
        """Return (module directories {rel: [files]}, extra executables [rel])."""
        modules, executables = {}, []
        for dirpath, dirnames, filenames in os.walk(root):
            rel = Path(dirpath).relative_to(root)
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".anyps5"))
            found = []
            for name in sorted(filenames):
                full = Path(dirpath) / name
                suffix = full.suffix.lower()
                if suffix in MODULE_EXTS and magic_of(full):
                    found.append(name)
                elif suffix == ".bin" and full.resolve() != eboot and magic_of(full):
                    executables.append((rel / name).as_posix())
            if found:
                modules[rel.as_posix()] = found
        return modules, executables

    # ---- downloads

    def github_release(self):
        tag = self.c.version.strip() or "latest"
        url = f"https://api.github.com/repos/{REPO}/releases/" + ("latest" if tag == "latest" else f"tags/{tag}")
        headers = {"User-Agent": "anyps5-converter", "Accept": "application/vnd.github+json"}
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                data = json.load(response)
        except Exception as error:
            cached = self.cached_tag(tag)
            if cached:
                self.log(f"Cannot reach GitHub ({error}); using cached version: {cached}")
                return cached, {}
            raise ConvertError(f"Cannot fetch release information for {tag}: {error}")
        return data["tag_name"], {asset["name"]: asset["browser_download_url"] for asset in data.get("assets", [])}

    def cached_tag(self, tag):
        cache = TOOL_DIR / "cache"
        if tag != "latest":
            return tag if (cache / tag).is_dir() else None
        tags = sorted((p.name for p in cache.glob("v*") if p.is_dir()), reverse=True) if cache.is_dir() else []
        return tags[0] if tags else None

    def download(self, assets, name, destination):
        if destination.is_file():
            return destination
        if name not in assets:
            raise ConvertError(f"The release does not contain {name} (and it is not cached).")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        self.log(f"Downloading {name} ...")
        request = urllib.request.Request(assets[name], headers={"User-Agent": "anyps5-converter"})
        with urllib.request.urlopen(request, timeout=60) as response, open(temporary, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                self.progress(f"Downloading {name}: {human(done)}" + (f" / {human(total)}" if total else ""))
        os.replace(temporary, destination)
        return destination

    def get_relinker(self, tag, assets):
        if self.c.relinker:
            path = Path(self.c.relinker).expanduser().resolve()
            if not path.is_file():
                raise ConvertError(f"Relinker not found: {path}")
            return path
        system = host_os()
        if system == "macos":
            raise ConvertError("AnyPS5 releases do not include a macOS relinker. Build it (docs/dev/BUILD.md, "
                               "-DANYPS5_RELINKER_ONLY=ON) and select it with --relinker / the 'Relinker' field.")
        if platform.machine().lower() not in ("amd64", "x86_64", "x64"):
            raise ConvertError("The prebuilt relinker is x86-64 only. Provide your own with --relinker.")
        name = f"relinker-{tag}.exe" if system == "windows" else f"relinker-{tag}"
        path = self.download(assets, name, TOOL_DIR / "cache" / tag / name)
        if system != "windows":
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def get_libs(self, tag, assets, target_os):
        if self.c.libs:
            path = Path(self.c.libs).expanduser().resolve()
            if (path / "libs").is_dir():
                path = path / "libs"
            if not any(path.glob("*.prx")):
                raise ConvertError(f"No *.prx libraries in {path}.")
            return path
        if target_os == "macos":
            raise ConvertError("AnyPS5 releases do not include macOS libraries. Build them (docs/dev/BUILD.md, "
                               "target 'libs') and select the folder with --libs / the 'Libraries' field.")
        name = f"prx-{target_os}-{tag}.zip"
        archive = self.download(assets, name, TOOL_DIR / "cache" / tag / name)
        extracted = TOOL_DIR / "cache" / tag / f"libs-{target_os}"
        if not extracted.is_dir():
            self.log(f"Extracting {name} ...")
            temporary = extracted.with_name(extracted.name + ".part")
            safe_remove(temporary)
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(temporary)
            os.replace(temporary, extracted)
        found = [p.parent for p in extracted.rglob("*.prx")]
        if not found:
            raise ConvertError(f"Archive {name} contains no libraries.")
        return min(found, key=lambda p: len(p.parts))

    # ---- staging

    def unwrap(self, source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        kind = magic_of(source)
        if kind == "elf":
            shutil.copy2(source, destination)
            return "ELF"
        try:
            unwrap_self.unwrap(str(source), str(destination))
        except unwrap_self.Refused as error:
            if "encrypted" in str(error):
                raise ConvertError(f"{source.name} is ENCRYPTED. You need a game dump decrypted on the "
                                   f"console (jailbroken PS5 with a dumper/SELF decrypter). Details: {error}")
            raise ConvertError(f"Cannot unwrap {source.name}: {error}")
        return "SELF -> ELF"

    # ---- app0 layout

    def link_dir(self, source, destination, mode):
        if mode == "link":
            try:
                if IS_WINDOWS:
                    import _winapi
                    _winapi.CreateJunction(str(source), str(destination))
                else:
                    os.symlink(source, destination, target_is_directory=True)
                return
            except OSError:
                self.note_copy()
        shutil.copytree(source, destination, copy_function=shutil.copy2)

    def link_file(self, source, destination, mode):
        if mode == "link":
            try:
                os.link(source, destination)
                return
            except OSError:
                try:
                    os.symlink(source, destination)
                    return
                except OSError:
                    self.note_copy()
        shutil.copy2(source, destination)

    def note_copy(self):
        if not self.copied_fallback:
            self.copied_fallback = True
            self.log("WARNING: linking is not possible here (other drive/no permission) - some files are copied.")

    def build_app0(self, root, app0, modules, mode):
        module_dirs = {Path(rel) for rel in modules}
        module_files = {Path(rel) / name for rel, names in modules.items() for name in names}

        def needs_real(rel):
            return any(rel == m or rel in m.parents for m in module_dirs)

        count = 0

        def mirror(source_dir, rel):
            nonlocal count
            (app0 / rel).mkdir(parents=True, exist_ok=True)
            for entry in sorted(os.scandir(source_dir), key=lambda e: e.name):
                item = rel / entry.name
                if rel == Path(".") and entry.name.startswith(".anyps5"):
                    continue
                if entry.is_dir():
                    if needs_real(item) and not is_link(entry.path):
                        mirror(entry.path, item)
                    else:
                        self.link_dir(entry.path, app0 / item, mode)
                elif item not in module_files:
                    self.link_file(entry.path, app0 / item, mode)
                count += 1
                if count % 200 == 0:
                    self.progress(f"Assembling app0: {count} items")

        mirror(root, Path("."))

    # ---- relinker

    def run_relinker(self, command, cwd):
        self.log("> " + " ".join(f'"{a}"' if " " in a else a for a in command))
        flags = 0x08000000 if IS_WINDOWS else 0
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=flags)
        buffer, tail, arrange, substitutions = b"", [], False, 0
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                tail = (tail + [line])[-40:]
                if re.search(r"\|\s*\d+%", line):
                    self.progress(re.sub(r"\s+", " ", line.split("%")[0]) + "%")
                    continue
                if line.startswith("Intel substitution:"):
                    substitutions += 1
                    if not self.c.verbose:
                        continue
                if line.startswith("Arrange the files"):
                    arrange = True
                if arrange and not self.c.verbose:
                    continue
                self.log("  " + line)
        code = process.wait()
        if code != 0:
            if buffer:
                self.log("  " + buffer.decode("utf-8", "replace"))
            raise ConvertError(f"The relinker failed (exit code {code}). See the last lines above.")

    # ---- main flow

    def convert(self):
        c = self.c
        started = time.time()
        system = host_os()
        target_os = c.target_os or system
        if target_os not in OS_CHOICES:
            raise ConvertError(f"Unknown OS: {target_os}")
        cpu = resolve_cpu(c.cpu, target_os)
        if cpu == "rosetta" and target_os == "windows":
            raise ConvertError("CPU mode 'rosetta' does not work with Windows.")
        mode = c.link_mode if c.link_mode != "auto" else ("link" if target_os == system else "copy")

        eboot, root = self.locate_game()
        title_id, title_name = self.read_title(root)
        self.log(f"Game: {title_name or '?'} ({title_id or '?'})  -  {root}")
        cpu_name, _ = cpu_info()
        self.log(f"Host: {system}, CPU: {cpu_name}  ->  target: {target_os}, CPU mode: {cpu}, app0: {mode}")

        base = re.sub(r"[^A-Za-z0-9_-]+", "", (title_name or "").title()) or title_id or "game"
        output = Path(c.output).expanduser() if c.output else root.parent / f"{root.name}_{target_os}"
        output = output.resolve()
        if is_inside(output, root) or is_inside(root, output):
            raise ConvertError("The output folder cannot be inside the game folder (or vice versa).")
        exe_name = base + {"windows": ".exe", "linux": ".elf", "macos": ""}[target_os]

        modules, found_executables = self.scan(root, eboot)
        extra_dirs = [rel for rel in modules if rel not in AUTO_MODULE_DIRS]
        self.log("Modules: " + (", ".join(f"{rel}/ ({len(n)})" for rel, n in modules.items()) or "none"))
        executables = list(c.executables)
        if found_executables:
            if c.include_executables:
                executables += [e for e in found_executables if e not in executables]
            else:
                self.log("Extra executables found (skipped; enable 'Include extra executables' if the game uses them): "
                         + ", ".join(found_executables))

        tag, assets = self.github_release()
        self.log(f"AnyPS5: {tag}")
        relinker = self.get_relinker(tag, assets)
        libs = self.get_libs(tag, assets, target_os)

        marker = output / MARKER
        if output.exists() and any(output.iterdir()):
            if marker.is_file():
                self.log("Output folder exists - rebuilding (save data and shader cache are kept).")
                previous = json.loads(marker.read_text(encoding="utf-8"))
                for name in ["app0", "libs", STAGE] + previous.get("outputs", []):
                    safe_remove(output / name)
            elif not c.force:
                raise ConvertError(f"Folder {output} is not empty. Choose another one or use --force.")
        output.mkdir(parents=True, exist_ok=True)

        stage = output / STAGE
        safe_remove(stage)
        self.log("Unwrapping executables ...")
        self.log(f"  eboot.bin: {self.unwrap(eboot, stage / 'eboot.elf')}")
        for rel, names in modules.items():
            for name in names:
                self.progress(f"Unwrapping {rel}/{name}")
                kind = self.unwrap(root / rel / name, stage / rel / name)
                if c.verbose:
                    self.log(f"  {rel}/{name}: {kind}")
        for rel in executables:
            self.unwrap(root / rel, stage / rel)

        self.log(f"Assembling app0 ({'links' if mode == 'link' else 'copying - this may take a while'}) ...")
        self.build_app0(root, output / "app0", modules, mode)

        command = [str(relinker)]
        command += {"windows": ["--windows"], "linux": [], "macos": ["--macos"]}[target_os]
        if c.windows_gui and target_os == "windows":
            command.append("--windows-gui")
        command += {"amd": [], "intel": ["--to-intel"], "rosetta": ["--to-rosetta"]}[cpu]
        for rel in extra_dirs:
            command += ["--module-dir", rel]
        for rel in executables:
            command += ["--executable", rel]
        command += list(c.extra_args)
        command += [str(stage / "eboot.elf"), str(output / exe_name)]
        self.log("Relinking ...")
        self.run_relinker(command, output)

        self.log("Copying AnyPS5 system libraries ...")
        safe_remove(output / "libs")
        shutil.copytree(libs, output / "libs")

        ext = Path(exe_name).suffix
        outputs = [exe_name] + [rel.replace("/", ".") + ext for rel in executables]
        if target_os != "windows" and system != "windows":
            for name in outputs:
                path = output / name
                if path.exists():
                    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if not c.keep_stage:
            safe_remove(stage)
        marker.write_text(json.dumps({
            "tool": VERSION, "anyps5": tag, "game": str(root), "title": title_name, "titleId": title_id,
            "target_os": target_os, "cpu": cpu, "app0": mode, "outputs": outputs,
            "converted": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        self.log("")
        self.log(f"DONE in {time.time() - started:.0f} s:  {output / exe_name}")
        self.notes(target_os, mode)
        self.progress("Done")
        if c.run_after:
            self.launch(output, exe_name, target_os)
        return output / exe_name

    def notes(self, target_os, mode):
        if mode == "link":
            self.log("app0/ links to the original game files - do not delete or modify the source folder.")
        if target_os == "windows":
            self.log("Windows: the game needs a lot of virtual memory (RAM + page file).")
        elif target_os == "linux":
            self.log("Linux: run ./<program>.elf; on a /dev/udmabuf error add your user to the 'kvm' group.")
            if host_os() == "windows":
                self.log("Linux: after copying to Linux run 'chmod +x' on the program file.")

    def launch(self, output, exe_name, target_os):
        if target_os != host_os():
            self.log(f"Not launching - the program is for {target_os}, this computer runs {host_os()}.")
            return
        self.log("Launching the game ...")
        program = str(output / exe_name)
        if IS_WINDOWS:
            subprocess.Popen([program], cwd=output, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            subprocess.Popen([program], cwd=output, start_new_session=True)


# ---------------------------------------------------------------- GUI

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title(f"AnyPS5 Converter {VERSION}")
    root.minsize(760, 560)
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    cpu_name, vendor = cpu_info()
    v = {
        "game": tk.StringVar(), "output": tk.StringVar(), "os": tk.StringVar(value=host_os()),
        "cpu": tk.StringVar(value="auto"), "version": tk.StringVar(value="latest"),
        "link": tk.StringVar(value="auto"), "run": tk.BooleanVar(), "gui": tk.BooleanVar(),
        "exes": tk.BooleanVar(), "relinker": tk.StringVar(), "libs": tk.StringVar(),
        "verbose": tk.BooleanVar(),
    }
    auto_output = {"value": ""}

    def suggest_output(*_):
        game = v["game"].get().strip()
        if not game or v["output"].get() not in ("", auto_output["value"]):
            return
        path = Path(game)
        folder = path.parent if path.is_file() else path
        auto_output["value"] = str(folder.parent / f"{folder.name}_{v['os'].get()}")
        v["output"].set(auto_output["value"])

    v["game"].trace_add("write", suggest_output)
    v["os"].trace_add("write", suggest_output)

    row = 0

    def path_row(label, var, buttons):
        nonlocal row
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", padx=5)
        box = ttk.Frame(frame)
        box.grid(row=row, column=2, sticky="e")
        for text, command in buttons:
            ttk.Button(box, text=text, command=command, width=8).pack(side="left", padx=1)
        row += 1

    def pick_dir(var):
        return lambda: var.set(filedialog.askdirectory() or var.get())

    def pick_file(var, types):
        return lambda: var.set(filedialog.askopenfilename(filetypes=types) or var.get())

    path_row("Game (folder / eboot.bin):", v["game"],
             [("Folder…", pick_dir(v["game"])), ("File…", pick_file(v["game"], [("eboot", "*.bin"), ("All files", "*")]))])
    path_row("Output folder:", v["output"], [("Folder…", pick_dir(v["output"]))])

    def combo_row(label, var, values, hint=""):
        nonlocal row
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
        box = ttk.Frame(frame)
        box.grid(row=row, column=1, columnspan=2, sticky="w", padx=5)
        ttk.Combobox(box, textvariable=var, values=values, state="readonly", width=12).pack(side="left")
        if hint:
            ttk.Label(box, text=hint, foreground="gray").pack(side="left", padx=8)
        row += 1

    combo_row("Target OS:", v["os"], OS_CHOICES, f"this computer: {host_os()}")
    combo_row("CPU:", v["cpu"], CPU_CHOICES, f"detected: {cpu_name or '?'} ({vendor})")
    combo_row("Game files in app0:", v["link"], LINK_CHOICES, "auto = links for this OS, copy for another OS")
    ttk.Label(frame, text="AnyPS5 version:").grid(row=row, column=0, sticky="w", pady=3)
    ttk.Entry(frame, textvariable=v["version"], width=14).grid(row=row, column=1, sticky="w", padx=5)
    row += 1

    checks = ttk.Frame(frame)
    checks.grid(row=row, column=0, columnspan=3, sticky="w", pady=4)
    for text, key in (("Run after conversion", "run"), ("No console window (Windows)", "gui"),
                      ("Include extra executables", "exes"), ("Verbose log", "verbose")):
        ttk.Checkbutton(checks, text=text, variable=v[key]).pack(side="left", padx=(0, 12))
    row += 1

    advanced = ttk.LabelFrame(frame, text="Advanced (optional, e.g. for macOS / your own build)", padding=6)
    advanced.grid(row=row, column=0, columnspan=3, sticky="ew", pady=4)
    advanced.columnconfigure(1, weight=1)
    for index, (label, key, picker) in enumerate((
            ("Relinker:", "relinker", pick_file(v["relinker"], [("All files", "*")])),
            ("Libraries (libs):", "libs", pick_dir(v["libs"])))):
        ttk.Label(advanced, text=label).grid(row=index, column=0, sticky="w")
        ttk.Entry(advanced, textvariable=v[key]).grid(row=index, column=1, sticky="ew", padx=5)
        ttk.Button(advanced, text="…", width=3, command=picker).grid(row=index, column=2)
    row += 1

    actions = ttk.Frame(frame)
    actions.grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
    status = tk.StringVar(value="Select a game and click Convert.")
    button = ttk.Button(actions, text="Convert")
    button.pack(side="left")
    ttk.Label(actions, textvariable=status).pack(side="left", padx=10)
    row += 1

    log = ScrolledText(frame, height=16, font=("Consolas", 9) if IS_WINDOWS else ("Monospace", 9))
    log.grid(row=row, column=0, columnspan=3, sticky="nsew")
    frame.rowconfigure(row, weight=1)

    messages = queue.Queue()

    def pump():
        try:
            while True:
                kind, text = messages.get_nowait()
                if kind == "log":
                    log.insert("end", text + "\n")
                    log.see("end")
                elif kind == "progress":
                    status.set(text[:110])
                elif kind == "done":
                    button.state(["!disabled"])
                    if text:
                        messagebox.showerror("Error", text)
                    else:
                        messagebox.showinfo("Done", "Conversion finished.")
        except queue.Empty:
            pass
        root.after(100, pump)

    def start():
        if not v["game"].get().strip():
            messagebox.showwarning("No game", "Select the game folder or the eboot.bin file.")
            return
        config = Config(game=v["game"].get().strip(), output=v["output"].get().strip(), target_os=v["os"].get(),
                        cpu=v["cpu"].get(), version=v["version"].get(), link_mode=v["link"].get(),
                        run_after=v["run"].get(), windows_gui=v["gui"].get(), include_executables=v["exes"].get(),
                        relinker=v["relinker"].get().strip(), libs=v["libs"].get().strip(), verbose=v["verbose"].get())
        button.state(["disabled"])
        log.delete("1.0", "end")

        def work():
            converter = Converter(config, log=lambda t: messages.put(("log", t)),
                                  progress=lambda t: messages.put(("progress", t)))
            try:
                converter.convert()
                messages.put(("done", ""))
            except ConvertError as error:
                messages.put(("log", f"ERROR: {error}"))
                messages.put(("progress", "Error"))
                messages.put(("done", str(error)))
            except Exception as error:
                messages.put(("log", f"ERROR: {type(error).__name__}: {error}"))
                messages.put(("progress", "Error"))
                messages.put(("done", f"{type(error).__name__}: {error}"))

        threading.Thread(target=work, daemon=True).start()

    button.configure(command=start)
    root.after(100, pump)
    root.mainloop()


# ---------------------------------------------------------------- CLI

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv == ["--gui"]:
        try:
            run_gui()
            return 0
        except ImportError:
            print("tkinter is missing (e.g. 'sudo apt install python3-tk'). Use the command line mode: --help")
            return 1
    parser = argparse.ArgumentParser(description="Convert a PS5 game into a native program with AnyPS5.")
    parser.add_argument("game", help="game folder (with eboot.bin) or the eboot.bin file")
    parser.add_argument("-o", "--output", default="", help="output folder (default <game folder>_<os>)")
    parser.add_argument("--os", dest="target_os", choices=OS_CHOICES, default="", help="target OS (default: this one)")
    parser.add_argument("--cpu", choices=CPU_CHOICES, default="auto", help="target CPU (default: detected)")
    parser.add_argument("--version", default="latest", help="AnyPS5 version, e.g. v0.1.2 (default: latest)")
    parser.add_argument("--link-mode", choices=LINK_CHOICES, default="auto", help="game files in app0: links/copy")
    parser.add_argument("--run", action="store_true", help="launch the game after conversion")
    parser.add_argument("--windows-gui", action="store_true", help="Windows program without a console window")
    parser.add_argument("--include-executables", action="store_true", help="include detected extra *.bin executables")
    parser.add_argument("--executable", action="append", default=[], help="extra executable (relative path)")
    parser.add_argument("--relinker", default="", help="custom relinker binary")
    parser.add_argument("--libs", default="", help="custom AnyPS5 libraries folder (*.prx)")
    parser.add_argument("--extra", action="append", default=[], help="extra relinker argument (repeatable)")
    parser.add_argument("--force", action="store_true", help="allow writing into a non-empty folder")
    parser.add_argument("--keep-stage", action="store_true", help="keep unwrapped ELF files (.anyps5-stage)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose log")
    args = parser.parse_args(argv)
    config = Config(game=args.game, output=args.output, target_os=args.target_os, cpu=args.cpu, version=args.version,
                    link_mode=args.link_mode, run_after=args.run, windows_gui=args.windows_gui,
                    include_executables=args.include_executables, relinker=args.relinker, libs=args.libs,
                    executables=args.executable, extra_args=args.extra, force=args.force,
                    keep_stage=args.keep_stage, verbose=args.verbose)
    last = {"text": ""}

    def progress(text):
        if sys.stdout.isatty():
            sys.stdout.write("\r" + text[:100].ljust(100))
            sys.stdout.flush()
            last["text"] = text

    def log(text):
        if last["text"]:
            sys.stdout.write("\r" + " " * 100 + "\r")
            last["text"] = ""
        print(text, flush=True)

    try:
        Converter(config, log=log, progress=progress).convert()
        return 0
    except ConvertError as error:
        log(f"ERROR: {error}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
