"""Dashboard configuration.

Precedence (first wins):
1. Environment variables ``PROM_URL`` / ``PROM_USER`` / ``PROM_PASSWORD``.
2. ``jobgraph.conf`` (shared with the jobgraph tool): ``$JOBGRAPH_CONFIG``,
   then ``/etc/jobgraph.conf``, then ``~/.config/jobgraph.conf``.
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


def load_config():
    url = os.environ.get("PROM_URL")
    user = os.environ.get("PROM_USER")
    password = os.environ.get("PROM_PASSWORD")
    timeout = int(os.environ.get("PROM_TIMEOUT", "30"))

    if not url:
        conf = {}
        for path in (
            os.environ.get("JOBGRAPH_CONFIG"),
            "/etc/jobgraph.conf",
            os.path.expanduser("~/.config/jobgraph.conf"),
        ):
            if path and os.path.isfile(path):
                conf = _read_jobgraph_conf(path)
                break
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
