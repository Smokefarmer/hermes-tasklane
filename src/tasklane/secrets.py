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
from pathlib import Path

#: Permission bits a secret env file must carry (owner read/write only).
_REQUIRED_MODE = 0o600


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
