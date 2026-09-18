"""Contact loader unit tests: local parsing, normalization, Git refresh.

Run: .venv/bin/python -m pytest tests/ -q
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import domain.contacts as contacts  # noqa: E402

HEADER = "day,realname,username,title,dept,helpers,summary\n"


def _write(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return str(tmp_path)


def _load_local(tmp_path, files):
    for name in ("GARAGE_DIARY_PATH", "GARAGE_DIARY_REPO",
                 "GARAGE_DIARY_CHECKOUT"):
        os.environ.pop(name, None)
    os.environ["GARAGE_DIARY_PATH"] = _write(tmp_path, files)
    return contacts.load_contacts()


def test_local_parses_and_normalizes(tmp_path):
    result = _load_local(tmp_path, {
        "diary.csv": HEADER +
        "20240105,X,Alice1;pBob@aalto.fi,p,d,h,Hello; world\n",
    })
    assert result["contacts"] == [
        {"date": "2024-01-05", "username": "pbob",
         "message": "Hello; world"},
        {"date": "2024-01-05", "username": "alice1",
         "message": "Hello; world"},
    ]


def test_all_diary_files_discovered(tmp_path):
    result = _load_local(tmp_path, {
        "diary.csv": HEADER + "20240102,X,alice1,p,d,h,New\n",
        "diary2020.csv": HEADER + "20200101,X,alice1,p,d,h,Old\n",
        "diary9999.txt": "not a diary file\n",
        "notes.txt": HEADER + "20240103,X,alice1,p,d,h,Ignored\n",
    })
    dates = [c["date"] for c in result["contacts"]]
    assert dates == ["2024-01-02", "2020-01-01"]


def test_dedup_and_deterministic_order(tmp_path):
    result = _load_local(tmp_path, {
        "diary.csv": HEADER + "20240102,X,bob,p,d,h,Same\n"
                              "20240102,X,alice1,p,d,h,Zeta\n"
                              "20240101,X,alice1,p,d,h,Zeta\n"
                              "20240101,X,alice1,p,d,h,Aardvark\n",
        "diary2020.csv": HEADER + "20240102,X,bob,p,d,h,Same\n",
    })
    assert [(c["date"], c["username"], c["message"])
            for c in result["contacts"]] == [
        ("2024-01-02", "bob", "Same"),
        ("2024-01-02", "alice1", "Zeta"),
        ("2024-01-01", "alice1", "Zeta"),
        ("2024-01-01", "alice1", "Aardvark"),
    ]


def test_malformed_rows_counted(tmp_path):
    result = _load_local(tmp_path, {
        "diary.csv": HEADER + "20240102,X,alice1,p,d,h,Ok\n"
                              "20241302,X,alice1,p,d,h,Bad month\n"
                              "22220325,X,alice1,p,d,h,Far future\n"
                              "20240102,X,,p,d,h,No user\n"
                              "20240102,X,?,p,d,h,Question only\n",
    })
    assert result["available"] is True
    assert result["skipped_rows"] == 4
    assert [c["message"] for c in result["contacts"]] == ["Ok"]


def test_question_and_empty_summary_preserved(tmp_path):
    result = _load_local(tmp_path, {
        "diary.csv": HEADER + "20240102,X,alice1,p,d,h,?\n"
                              "20240103,X,alice1,p,d,h,\n",
    })
    # Descending date within the same user; empty summary stays empty.
    assert [(c["date"], c["message"]) for c in result["contacts"]] == [
        ("2024-01-03", ""), ("2024-01-02", "?")]

def test_missing_directory(tmp_path):
    for name in ("GARAGE_DIARY_PATH", "GARAGE_DIARY_REPO",
                 "GARAGE_DIARY_CHECKOUT"):
        os.environ.pop(name, None)
    os.environ["GARAGE_DIARY_PATH"] = str(tmp_path / "nope")
    result = contacts.load_contacts()
    assert result == {
        "available": False,
        "warning": "Garage Diary local path is unavailable.",
        "skipped_rows": 0, "contacts": [],
    }


def test_no_csv_files(tmp_path):
    result = _load_local(tmp_path, {"readme.md": "nothing\n"})
    assert result["warning"] == "Garage Diary contains no diary CSV files."


def test_bad_schema(tmp_path):
    result = _load_local(tmp_path, {"diary.csv": "date,who\n20240102,a\n"})
    assert result["warning"] == "Garage Diary CSV schema is invalid."


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def remote_repo(tmp_path):
    """A bare origin plus a seed repository to push commits to."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "--bare", "-b", "master", str(origin))
    _git("init", "-q", "-b", "master", str(seed))
    (seed / "diary.csv").write_text(HEADER + "20240101,X,alice1,p,d,h,V1\n")
    _git("-C", str(seed), "add", ".")
    _git("-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "v1")
    _git("-C", str(seed), "remote", "add", "origin", str(origin))
    _git("-C", str(seed), "push", "-q", "origin", "master")
    return str(origin)


def _remote_env(tmp_path, origin, checkout):
    for name in ("GARAGE_DIARY_PATH", "GARAGE_DIARY_CHECKOUT"):
        os.environ.pop(name, None)
    os.environ["GARAGE_DIARY_REPO"] = str(origin)
    os.environ["GARAGE_DIARY_CHECKOUT"] = str(checkout)


def test_remote_first_clone_and_fast_forward(tmp_path, monkeypatch,
                                             remote_repo):
    checkout = tmp_path / "co"
    _remote_env(tmp_path, remote_repo, checkout)

    result = contacts.load_contacts()
    assert result["available"] is True
    assert [c["message"] for c in result["contacts"]] == ["V1"]

    # Advance the origin; the next load must fast-forward and show V2.
    seed = tmp_path / "seed"
    (seed / "diary.csv").write_text(HEADER + "20240101,X,alice1,p,d,h,V1\n"
                                              "20240102,X,bob,p,d,h,V2\n")
    _git("-C", str(seed), "add", ".")
    _git("-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "v2")
    _git("-C", str(seed), "push", "-q", "origin", "master")
    result = contacts.load_contacts()
    assert [c["message"] for c in result["contacts"]] == ["V2", "V1"]


def test_remote_origin_mismatch(tmp_path, monkeypatch, remote_repo):
    checkout = tmp_path / "co"
    _remote_env(tmp_path, remote_repo, checkout)
    assert contacts.load_contacts()["available"] is True

    # Point the checkout at a different origin: refresh must refuse.
    _git("-C", str(checkout), "remote", "set-url", "origin",
         "git@elsewhere:x.git")
    result = contacts.load_contacts()
    assert result == {
        "available": False,
        "warning":
            "Garage Diary checkout origin does not match configured "
            "repository.",
        "skipped_rows": 0, "contacts": [],
    }


def test_remote_pull_failure_no_stale_contacts(tmp_path, monkeypatch,
                                               remote_repo):
    """A checkout behind a broken origin reports unavailable and never
    serves the stale rows still sitting in the checkout."""
    checkout = tmp_path / "co"
    _remote_env(tmp_path, remote_repo, checkout)
    assert contacts.load_contacts()["available"] is True
    # Write a stale row into the checkout, then sever the origin: the
    # pull fails and the stale row must NOT be served.
    (checkout / "diary.csv").write_text(
        HEADER + "20240101,X,old,p,d,h,Old\n")
    _git("-C", str(checkout), "add", ".")
    _git("-C", str(checkout), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "stale")
    import shutil
    shutil.rmtree(remote_repo)
    result = contacts.load_contacts()
    assert result == {
        "available": False, "warning": "Garage Diary Git refresh failed.",
        "skipped_rows": 0, "contacts": [],
    }


def test_remote_unreachable(tmp_path, monkeypatch):
    _remote_env(tmp_path, tmp_path / "definitely-missing.git",
                tmp_path / "co")
    result = contacts.load_contacts()
    assert result == {
        "available": False, "warning": "Garage Diary Git refresh failed.",
        "skipped_rows": 0, "contacts": [],
    }


def test_remote_git_timeout(tmp_path, monkeypatch, remote_repo):
    """A git call that exceeds the timeout must degrade to an unavailable
    source with the fixed refresh-failure warning — and never serve the
    stale rows still sitting in the checkout."""
    checkout = tmp_path / "co"
    _remote_env(tmp_path, remote_repo, checkout)
    assert contacts.load_contacts()["available"] is True
    # Replace subprocess.run so the REAL _git's try/except runs: a hung
    # git call must be swallowed there and mapped to a failed refresh.
    real_run = subprocess.run

    def hung(args, **kwargs):
        if args and args[0] == "git":
            raise subprocess.TimeoutExpired(
                cmd=args, timeout=contacts.GIT_TIMEOUT_S)
        return real_run(args, **kwargs)

    monkeypatch.setattr(contacts.subprocess, "run", hung)
    result = contacts.load_contacts()
    assert result == {
        "available": False, "warning": "Garage Diary Git refresh failed.",
        "skipped_rows": 0, "contacts": [],
    }

def test_git_timeout_constant():
    # Git calls must be bounded; a hung fetch cannot block the dashboard.
    assert contacts.GIT_TIMEOUT_S == 30
