"""
Terminal prompts and output, with no dependencies.

Colour is opt-out via NO_COLOR and is disabled automatically when stdout is not
a terminal, so piping `mmu doctor` into a bug report produces clean text.
"""

import os
import sys


def _colour_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Windows 10+ understands ANSI once virtual terminal processing is on.
        # Enabling it can fail on older consoles; colour is cosmetic, so a
        # failure just means plain text.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


_COLOUR = _colour_enabled()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def bold(t):  return _c("1", t)
def dim(t):   return _c("2", t)
def red(t):   return _c("31", t)
def green(t): return _c("32", t)
def yellow(t): return _c("33", t)
def cyan(t):  return _c("36", t)


def header(text):
    print()
    print(bold(text))
    print(dim("-" * len(text)))


def step(text):
    print(f"  {cyan('>')} {text}")


def ok(text):
    print(f"  {green('OK')}  {text}")


def warn(text):
    print(f"  {yellow('!!')}  {text}")


def fail(text):
    print(f"  {red('XX')}  {text}")


def info(text):
    print(f"      {dim(text)}")


def ask(prompt, default=None, secret=False):
    """
    Prompt for a value. Returns the default on an empty answer.

    Non-interactive runs (a CI build, a piped script) never reach here -- the
    caller checks `interactive()` first -- but if stdin is closed underneath us
    mid-run, treat that as accepting the default rather than crashing.
    """
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            raw = getpass.getpass(f"  {prompt}{suffix}: ")
        else:
            raw = input(f"  {prompt}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    raw = raw.strip()
    return raw if raw else default


def confirm(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    return raw.startswith("y")


def interactive():
    return sys.stdin is not None and sys.stdin.isatty()
