"""Dashboard configuration.

1. Environment variables ``PROM_URL`` / ``PROM_USER`` / ``PROM_PASSWORD``.
2. ``jobgraph.conf`` (shared with the jobgraph tool): ``$JOBGRAPH_CONFIG``,
   then ``/etc/jobgraph.conf``, then ``~/.config/jobgraph.conf``.

Garage Diary contact source (``GARAGE_DIARY_PATH`` / ``GARAGE_DIARY_REPO`` /
``GARAGE_DIARY_CHECKOUT``) follows the same precedence over the
``garage_diary_*`` keys in ``jobgraph.conf``; see ``load_contact_config``.
"""

import configparser
import os


class ConfigError(Exception):
    pass


def _read_jobgraph_conf(path):
    parser = configparser.ConfigParser()
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return {}
    # Accept both bare "key = value" files and [section] files.
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        parser.read_string("[jobgraph]\n" + text)
    section = {}
    if parser.defaults():
        section.update(parser.defaults())
    for name in parser.sections():
        section.update(parser.items(name))
    return section


def _jobgraph_conf():
    for path in (
        os.environ.get("JOBGRAPH_CONFIG"),
        "/etc/jobgraph.conf",
        os.path.expanduser("~/.config/jobgraph.conf"),
    ):
        if path and os.path.isfile(path):
            return _read_jobgraph_conf(path)
    return {}


def load_config():
    url = os.environ.get("PROM_URL")
    user = os.environ.get("PROM_USER")
    password = os.environ.get("PROM_PASSWORD")
    timeout = int(os.environ.get("PROM_TIMEOUT", "30"))

    if not url:
        conf = _jobgraph_conf()
        url = url or conf.get("prom_url")
        user = user or conf.get("username")
        password = password or conf.get("password")
        timeout = int(conf.get("timeout", timeout))

    if not url:
        raise ConfigError(
            "Prometheus URL not configured. Set PROM_URL or provide jobgraph.conf."
        )
    if not url.endswith("/api/v1"):
        url = url.rstrip("/") + "/api/v1"

    return {
        "api_base": url,
        "username": user or "",
        "password": password or "",
        "timeout": timeout,
    }


def load_contact_config():
    """Resolve the Garage Diary contact source.

    Returns ``{mode, path, repo, checkout}`` where ``mode`` is ``local``,
    ``remote``, ``unconfigured`` or ``ambiguous``. ``path`` and ``repo``
    are mutually exclusive; setting both yields ``ambiguous`` — never a
    silent pick.
    """
    # The source (PATH/REPO) resolves by layer: any source env var makes
    # the environment the source layer, shadowing the file's PATH/REPO
    # keys entirely. CHECKOUT is an independent knob — an env CHECKOUT
    # overrides the file's checkout even when the source comes from the
    # file (so a deployment can relocate the cache without copying the
    # source key too).
    source_from_env = bool(
        os.environ.get("GARAGE_DIARY_PATH")
        or os.environ.get("GARAGE_DIARY_REPO")
    )
    conf = {} if source_from_env else _jobgraph_conf()

    def _pick(env, key):
        value = os.environ.get(env) or conf.get(key) or ""
        return value.strip()

    path = _pick("GARAGE_DIARY_PATH", "garage_diary_path")
    repo = _pick("GARAGE_DIARY_REPO", "garage_diary_repo")
    checkout = _pick("GARAGE_DIARY_CHECKOUT", "garage_diary_checkout")

    if path and repo:
        return {"mode": "ambiguous", "path": path, "repo": repo,
                "checkout": checkout}
    if path:
        return {"mode": "local", "path": path, "repo": "", "checkout": ""}
    if repo:
        return {
            "mode": "remote",
            "path": "",
            "repo": repo,
            "checkout": checkout
            or os.path.expanduser(
                os.path.join("~", ".cache", "gpu-statistics", "garagediary")
            ),
        }
    return {"mode": "unconfigured", "path": "", "repo": "", "checkout": ""}
