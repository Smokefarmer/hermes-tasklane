"""Secret env-file loading for TaskLane tester jobs.

Tester roles need real credentials (test accounts, staging URLs) to drive an app
end-to-end. Those secrets live in a dotenv-style file **outside** the job store and
are injected straight into the ``claude`` subprocess environment — they must never
reach a job spec, prompt, log, or final response (all of which are persisted as
plain files on disk).

``load_env_file`` is deliberately strict: it refuses any file that is not mode
``0600`` or not owned by the current user, so a world-readable or shared secret
file fails loudly instead of leaking.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from tasklane.paths import tasklane_home

#: Permission bits a secret env file must carry (owner read/write only).
_REQUIRED_MODE = 0o600

#: Owner-only permission bits for the directory holding generated secret files.
_SECRETS_DIR_MODE = 0o700

#: A secret key must be a SCREAMING_SNAKE_CASE identifier (env-var safe).
SECRET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: Upper bound on a single secret value's length.
MAX_SECRET_VALUE_LENGTH = 4096
#: Upper bound on how many keys a single intake call may carry.
MAX_SECRET_KEYS = 50


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a dotenv-style secret file into a ``{KEY: VALUE}`` mapping.

    Format: ``KEY=VALUE`` per line. Blank lines and ``#`` comments are ignored.
    A leading ``export `` prefix is tolerated; surrounding single/double quotes
    around the value are stripped.

    Security contract — this function REFUSES (raises) rather than returning
    secrets from an insecurely-stored file:
      * ``FileNotFoundError`` if the path does not exist or is not a regular file.
      * ``PermissionError`` if the file is not owned by the current user.
      * ``PermissionError`` if the file's permission bits are not exactly ``0600``.

    Raises:
        ValueError: on a malformed line (no ``=`` separator or empty key).
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"env file not found: {file_path}")

    info = file_path.stat()
    current_uid = os.getuid()
    if info.st_uid != current_uid:
        raise PermissionError(
            f"refusing to read env file owned by uid {info.st_uid}, not the current user "
            f"(uid {current_uid}): {file_path}"
        )
    file_mode = stat.S_IMODE(info.st_mode)
    if file_mode != _REQUIRED_MODE:
        raise PermissionError(
            f"refusing to read env file with insecure permissions {oct(file_mode)}; "
            f"expected {oct(_REQUIRED_MODE)} (chmod 600): {file_path}"
        )

    env: dict[str, str] = {}
    for lineno, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ValueError(f"malformed env line {lineno} (no '='): {file_path}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"malformed env line {lineno} (empty key): {file_path}")
        env[key] = _parse_value(value.strip())
    return env


def _parse_value(value: str) -> str:
    """Return the env value: quoted values keep their contents verbatim; unquoted
    values have any trailing ``  # inline comment`` stripped (dotenv convention)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


# --------------------------------------------------------------------------- #
# conversational credential intake (set_project_secrets)
# --------------------------------------------------------------------------- #
def validate_secret_pairs(secrets: Mapping[str, str]) -> None:
    """Validate a ``{KEY: VALUE}`` intake mapping, raising ``ValueError`` on any
    violation. Error messages reference key NAMES only — never values — so they are
    safe to surface to a client or (indirectly) an audit log.

    Rules: at least one and at most :data:`MAX_SECRET_KEYS` keys; every key matches
    :data:`SECRET_KEY_PATTERN`; every value is a non-empty string no longer than
    :data:`MAX_SECRET_VALUE_LENGTH` and free of newlines (a dotenv line cannot hold one).
    """
    if not isinstance(secrets, Mapping):
        raise ValueError("secrets must be a mapping of KEY=VALUE pairs")
    if not secrets:
        raise ValueError("no secrets provided")
    if len(secrets) > MAX_SECRET_KEYS:
        raise ValueError(f"too many keys: {len(secrets)} (max {MAX_SECRET_KEYS})")
    for key, value in secrets.items():
        if not isinstance(key, str) or not SECRET_KEY_PATTERN.match(key):
            raise ValueError(f"invalid secret key name: {key!r} (must match {SECRET_KEY_PATTERN.pattern})")
        if not isinstance(value, str) or value == "":
            raise ValueError(f"secret {key} has an empty or non-string value")
        if len(value) > MAX_SECRET_VALUE_LENGTH:
            raise ValueError(f"secret {key} value exceeds {MAX_SECRET_VALUE_LENGTH} chars")
        if "\n" in value or "\r" in value:
            raise ValueError(f"secret {key} value contains an unsupported newline character")


def default_secret_file_path(project_name: str) -> Path:
    """Return the default secret file location for *project_name*:
    ``$TASKLANE_HOME/secrets/<sanitized-name>.env``. The path is returned, not created."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_name).strip("-._") or "project"
    return tasklane_home() / "secrets" / f"{safe}.env"


def write_secret_file(path: str | os.PathLike[str], values: Mapping[str, str], *,
                      secure_parent: bool = False) -> Path:
    """Write *values* to *path* as a ``KEY="VALUE"`` dotenv file at mode ``0600``.

    Values are always double-quoted so :func:`load_env_file` round-trips them
    verbatim (spaces, ``#``, ``=`` and embedded quotes are preserved). The write is
    atomic (temp file + ``os.replace``) so a reader never sees a partial/insecure file.
    The parent directory is secured to mode ``0700`` when *secure_parent* is true
    (the generated ``$TASKLANE_HOME/secrets`` dir) or whenever this call has to
    create it — a freshly created secrets directory is never left at the umask
    default. An already-existing parent's permissions are left untouched.
    """
    file_path = Path(path).expanduser()
    parent = file_path.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if secure_parent or not parent_existed:
        os.chmod(parent, _SECRETS_DIR_MODE)

    body = ["# Managed by TaskLane (set_project_secrets). Secret values — do not commit.\n"]
    for key in sorted(values):
        body.append(f'{key}="{values[key]}"\n')

    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".secret-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(body))
        os.chmod(tmp_name, _REQUIRED_MODE)
        os.replace(tmp_name, file_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return file_path
