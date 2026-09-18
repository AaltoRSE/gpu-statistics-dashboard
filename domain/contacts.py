"""Garage Diary contact-history loader.

Reads ``diary.csv`` / ``diaryYYYY.csv`` from either a local checkout
(``garage_diary_path``) or a dashboard-owned Git checkout
(``garage_diary_repo`` cloned/fetched into ``garage_diary_checkout``) and
returns normalized contact records: ``{date, username, message}`` with ISO
dates, lowercase usernames, and the verbatim ``summary`` text.

The Git refresh is synchronous: a missing checkout is cloned into a
temporary sibling and atomically installed; an existing one is verified
against the configured origin and fast-forwarded. Any failure is reported
as an unavailable source — stale rows are never served as current.
"""

import csv
import datetime
import os
import re
import shutil
import subprocess
import threading

from config import load_contact_config

GIT_TIMEOUT_S = 30

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_AALTO_SUFFIX = "@aalto.fi"

_LOCK = threading.Lock()

# Exact, credential-free warnings; never embed paths, URLs, or Git stderr.
_WARN_UNCONFIGURED = "Garage Diary source is not configured."
_WARN_LOCAL_MISSING = "Garage Diary local path is unavailable."
_WARN_NO_FILES = "Garage Diary contains no diary CSV files."
_WARN_SCHEMA = "Garage Diary CSV schema is invalid."
_WARN_GIT = "Garage Diary Git refresh failed."
_WARN_ORIGIN = "Garage Diary checkout origin does not match configured repository."

_WARN_UNCONFIGURED = "Garage Diary source is not configured."
_WARN_AMBIGUOUS = "Garage Diary source configuration is ambiguous."


def _diary_files(directory):
    """All diary*.csv in ``directory`` in filename order (diary.csv first)."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        os.path.join(directory, name)
        for name in names
        # diary.csv plus one file per diary year (diaryYYYY.csv, per the
        # approved discovery rule).
        if name == "diary.csv" or re.fullmatch(r"diary\d{4}\.csv", name)
    ]


def _git(args, cwd=None):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # A hung fetch must not crash the route or freeze the loader: a
        # timed-out refresh is an unavailable source, never stale rows.
        return subprocess.CompletedProcess(args, returncode=124)
    except OSError:
        # A missing git executable or spawn failure reduces to the same
        # fixed refresh-failure warning at every call site.
        return subprocess.CompletedProcess(args, returncode=124)


def _origin_url(checkout):
    result = _git(["-C", checkout, "remote", "get-url", "origin"])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _sync_remote_checkout(repo, checkout):
    """Clone or fast-forward ``checkout`` from ``repo``.

    Returns a fixed warning string on failure, None on success. The caller
    holds ``_LOCK``; a failed clone removes its temporary directory so a
    partial checkout is never installed. A timed-out git call (rc 124 from
    the _git timeout guard) is an unavailable source, not an origin
    mismatch.
    """
    checkout = os.path.expanduser(checkout)
    tmp = checkout + ".tmp-" + str(os.getpid())
    try:
        parent = os.path.dirname(checkout) or "."
        os.makedirs(parent, exist_ok=True)
        if not os.path.isdir(checkout):
            shutil.rmtree(tmp, ignore_errors=True)
            result = _git(["clone", repo, tmp])
            if result.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                return _WARN_GIT
            if os.path.exists(checkout):
                # A concurrent process won the race; drop ours and
                # refresh below.
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                os.replace(tmp, checkout)
    except OSError:
        # An unwritable parent, an unremovable temp, or a failed atomic
        # install must degrade to the fixed unavailable response — never
        # a 500 — and never leave a partial checkout behind.
        shutil.rmtree(tmp, ignore_errors=True)
        return _WARN_GIT
    origin = _origin_url(checkout)
    if origin is None or origin != repo:
        # A timed-out origin lookup (rc 124) returns None too — treat it
        # as a refresh failure, not an origin mismatch.
        return _WARN_GIT if origin is None else _WARN_ORIGIN
    result = _git(["-C", checkout, "pull", "--ff-only"])
    if result.returncode != 0:
        return _WARN_GIT
    return None


def _normalize_usernames(cell):
    """Split a username cell into normalized tokens; may be empty."""
    tokens = []
    for token in re.split(r"[\s]*;[\s]*", cell.strip()):
        # casefold, not lower: the plan requires Unicode-aggressive
        # folding so tokens like 'ſuser' (long s) join 'suser'.
        token = token.strip().casefold()
        if token.endswith(_AALTO_SUFFIX):
            token = token[: -len(_AALTO_SUFFIX)].strip()
        if token and _USERNAME_RE.match(token):
            tokens.append(token)
    return tokens


def _parse_diary_files(paths):
    """Parse the given CSV files; returns (contacts, skipped_rows), or None
    when a file lacks the required schema or cannot be read."""
    seen = set()
    contacts = []
    skipped = 0
    for path in paths:
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames or not {
                    "day", "username", "summary"
                } <= set(reader.fieldnames):
                    return None
                for row in reader:
                    day = (row.get("day") or "").strip()
                    if not re.fullmatch(r"\d{8}", day):
                        skipped += 1
                        continue
                    try:
                        date = datetime.datetime.strptime(
                            day, "%Y%m%d"
                        ).date()
                    except ValueError:
                        skipped += 1
                        continue
                    # Strict YYYYMMDD within the diary's plausible era: a
                    # typo like 22220325 parses as a real date but cannot
                    # be a diary entry.
                    if not 2000 <= date.year <= 2099:
                        skipped += 1
                        continue
                    date = date.isoformat()
                    usernames = _normalize_usernames(row.get("username") or "")
                    if not usernames:
                        skipped += 1
                        continue
                    message = (row.get("summary") or "").strip()
                    for username in usernames:
                        key = (date, username, message)
                        if key not in seen:
                            seen.add(key)
                            contacts.append(
                                {"date": date, "username": username,
                                 "message": message})
        except OSError:
            return None
    contacts.sort(key=lambda c: (c["date"], c["username"], c["message"]),
                  reverse=True)
    return contacts, skipped


def _unavailable(warning):
    return {"available": False, "warning": warning, "skipped_rows": 0,
            "contacts": []}


def _finish(paths):
    if not paths:
        return {"available": False, "warning": _WARN_NO_FILES,
                "skipped_rows": 0, "contacts": []}
    parsed = _parse_diary_files(paths)
    if parsed is None:
        return {"available": False, "warning": _WARN_SCHEMA,
                "skipped_rows": 0, "contacts": []}
    contacts, skipped = parsed
    return {"available": True, "warning": None, "skipped_rows": skipped,
            "contacts": contacts}


def _load_local(path):
    if not os.path.isdir(path):
        return _unavailable(_WARN_LOCAL_MISSING)
    return _finish(_diary_files(path))


def load_contacts():
    """Normalized Garage Diary contacts for the configured source.

    Returns ``{available, warning, skipped_rows, contacts}``. Never raises
    for an unusable source; the rest of the dashboard keeps working.
    """
    cfg = load_contact_config()
    if cfg["mode"] == "local":
        return _load_local(cfg["path"])
    if cfg["mode"] == "remote":
        with _LOCK:
            warning = _sync_remote_checkout(cfg["repo"], cfg["checkout"])
        if warning:
            return _unavailable(warning)
        return _finish(_diary_files(os.path.expanduser(cfg["checkout"])))
    if cfg["mode"] == "ambiguous":
        return _unavailable(_WARN_AMBIGUOUS)
    return _unavailable(_WARN_UNCONFIGURED)
