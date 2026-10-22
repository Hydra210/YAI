"""Child-process runner for Yuki's run_python skill.

Started as:  python -I sandbox_runner.py <workdir> <mem_mb>   (cwd = workdir, empty environment)

It reads <workdir>/main.py, installs a policy with sys.addaudithook(), then executes the code.
The policy (best effort, defense in depth, NOT a real sandbox):

  - no network (socket.*), no spawning processes (subprocess, os.system, fork, exec), no ctypes
  - files can only be written inside <workdir>
  - files can only be read inside <workdir> and the Python install (stdlib / site-packages / zoneinfo / fonts)
    so the bot's source, database file, .env, /etc, /proc etc. are not readable
  - no symlinks / hardlinks (so a link in <workdir> can't point at something outside it)
  - native extension modules can only be loaded from the Python install

Audit hooks can't be removed once installed, but a determined attacker may still find a way around them.
If you need real isolation run the bot in a container / VM with nothing valuable in it.
"""

import os
import sys

WORK = os.path.realpath(sys.argv[1])
MEM_MB = int(sys.argv[2]) if len(sys.argv) > 2 else 600
MAX_WRITE_OPENS = 40

try:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (MEM_MB * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except Exception:  # not on linux / not allowed: the parent still enforces a wall clock timeout
    pass


def _real(p):
    return os.path.realpath(os.fsdecode(p))


def _under(path, roots):
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _system_roots():
    import sysconfig

    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = sysconfig.get_path(key)
        if p:
            roots.add(p)
    roots.update(p for p in sys.path if p and os.path.isabs(p))
    roots.update(("/usr/share/zoneinfo", "/usr/share/fonts", "/usr/local/share/fonts", "/usr/lib/python3"))
    return {_real(r) for r in roots if r and os.path.exists(r)}


READ_ROOTS = _system_roots() | {WORK}
READ_FILES = {"/dev/null", "/dev/zero", "/dev/urandom", "/dev/random"}
WRITE_FILES = {"/dev/null"}

# modules whose import is refused outright (the audit events would catch most uses anyway)
BLOCKED_IMPORTS = {
    "ctypes", "_ctypes", "socket", "_socket", "ssl", "_ssl", "subprocess", "_posixsubprocess",
    "multiprocessing", "_multiprocessing", "pty", "resource",
}
# audit events that are always refused
BLOCKED_PREFIXES = (
    "socket.", "subprocess.", "ctypes.", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.forkpty", "os.kill", "os.killpg", "os.symlink", "os.link", "os.chown",
    "os.chroot", "os.setuid", "os.setgid", "os.mkfifo", "os.mknod",
    "os.startfile", "os.add_dll_directory", "shutil.chown", "pty.",
    "sys.setprofile", "sys.settrace", "winreg.", "webbrowser.open", "msvcrt.",
    "ftplib.", "smtplib.", "telnetlib.", "urllib.Request", "http.client.", "imaplib.", "poplib.",
    "nntplib.", "syslog.", "resource.setrlimit", "resource.prlimit",
)

_write_opens = 0


def _deny(why):
    raise PermissionError(f"blocked by yuki's sandbox: {why}")


def _is_write_mode(mode, flags):
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
        return True
    return False


def _hook(event, args):
    global _write_opens
    if event.startswith(BLOCKED_PREFIXES):
        _deny(event)

    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        if isinstance(path, int):  # already-open fd (e.g. from os.open we saw earlier)
            return
        p = _real(path)
        if _is_write_mode(mode, flags):
            if p in WRITE_FILES:
                return
            if not _under(p, {WORK}):
                _deny("can only write files inside the working folder")
            _write_opens += 1
            if _write_opens > MAX_WRITE_OPENS:
                _deny("too many files written")
        elif p not in READ_FILES and not _under(p, READ_ROOTS):
            _deny("can't read files outside the working folder")
        return

    if event in ("os.listdir", "os.scandir"):
        path = args[0]
        if path is None:
            return
        if isinstance(path, int):
            return
        if not _under(_real(path), READ_ROOTS):
            _deny("can't list folders outside the working folder")
        return

    if event in ("os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.truncate", "os.utime", "os.chmod", "shutil.rmtree",
                 "shutil.copyfile", "shutil.move", "shutil.copytree", "shutil.make_archive", "shutil.unpack_archive"):
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)) and not _under(_real(a), {WORK}):
                _deny("can only change files inside the working folder")
        return

    if event == "os.chdir":
        if not _under(_real(args[0]), {WORK}):
            _deny("can't leave the working folder")
        return

    if event == "import":
        module, filename = args[0], args[1]
        if str(module).split(".")[0] in BLOCKED_IMPORTS:
            _deny(f"importing {module}")
        if filename and str(filename).endswith((".so", ".pyd", ".dylib")) and not _under(
            _real(filename), READ_ROOTS - {WORK}
        ):
            _deny("loading native extension modules")
        return


def main():
    src_path = os.path.join(WORK, "main.py")
    with open(src_path, encoding="utf-8") as f:
        source = f.read()
    code = compile(source, "main.py", "exec")

    # make sure stuff the policy or tracebacks need is imported before the hook goes live
    import linecache  # noqa: F401
    import traceback

    if "PIL" in source:
        # Pillow's plugins import subprocess (EPS) at load time. Load them now, before the policy exists, then make
        # sure subprocess can't actually spawn anything: Popen is blocked by the audit event, and the C helper
        # module stays un-importable (its import is refused) and unreachable from the subprocess module.
        try:
            from PIL import Image

            Image.init()
            from PIL import ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageSequence  # noqa: F401
        except Exception:
            pass
        sub = sys.modules.get("subprocess")
        if sub is not None:
            sys.modules.pop("_posixsubprocess", None)
            for attr in ("_posixsubprocess", "_fork_exec"):
                if hasattr(sub, attr):
                    setattr(sub, attr, None)

    sys.addaudithook(_hook)
    scope = {"__name__": "__main__", "__file__": "main.py", "__builtins__": __builtins__}
    sys.argv = ["main.py"]
    try:
        exec(code, scope)
    except SystemExit as e:
        code_ = e.code
        sys.stdout.flush()
        sys.exit(code_ if isinstance(code_, int) else (0 if code_ is None else 1))
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
