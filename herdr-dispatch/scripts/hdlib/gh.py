import json
import subprocess

from . import util


def gh_json(args, cwd=None, timeout=120):
    r = subprocess.run(["gh", *args], capture_output=True, **util.TEXT, cwd=cwd, timeout=timeout)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def viewer_login():
    r = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, **util.TEXT)
    return r.stdout.strip() or None
