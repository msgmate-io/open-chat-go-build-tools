"""Git credential discovery for private integration repositories.

Local development runs the manager both on the host and inside the one-shot
``integration-sync`` container that the dev compose starts before backend and
frontend. That container has no credentials of its own, so before any private
clone it mirrors what CI does on the runner: make git authenticate with the
token (or forwarded SSH agent) the developer already uses.

Credentials are discovered in this order when the host home is mounted (the dev
compose mounts it read-only at ``/host-home`` via ``OPENCHAT_HOST_HOME``):

1. ``GITHUB_TOKEN`` / ``GH_TOKEN`` environment variables.
2. A GitHub token in the host's ``gh`` config (``~/.config/gh/hosts.yml``).
3. A GitHub token in the host's ``.git-credentials`` store.
4. A forwarded SSH agent (``SSH_AUTH_SOCK``) with the host's ``~/.ssh``.

The rewrite is injected through git's ``GIT_CONFIG_{COUNT,KEY_0,VALUE_0}``
environment variables, which every ``git`` subprocess inherits. Nothing is
written to any ``.gitconfig``, so running the manager on the host never mutates
the developer's git configuration.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _host_home() -> Optional[Path]:
    """The mounted host home, or ``None`` when not running in the dev container."""
    override = os.environ.get("OPENCHAT_HOST_HOME")
    if override:
        return Path(override)
    return None


def _token_from_env() -> Optional[str]:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _token_from_gh_config(home: Path) -> Optional[str]:
    path = home / ".config" / "gh" / "hosts.yml"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r"\s*oauth_token:\s*(\S+)\s*$", line)
        if match:
            return match.group(1)
    return None


def _token_from_credentials(home: Path) -> Optional[str]:
    path = home / ".git-credentials"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r"https://[^:/@]+:([^@]+)@github\.com/?$", line.strip())
        if match:
            return match.group(1)
    return None


def _ssh_agent_available() -> bool:
    socket = os.environ.get("SSH_AUTH_SOCK")
    if not socket or not Path(socket).exists():
        return False
    if shutil.which("ssh-add") is None:
        return False
    result = subprocess.run(
        ["ssh-add", "-l"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _inject_rewrite(key: str, value: str) -> None:
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = key
    os.environ["GIT_CONFIG_VALUE_0"] = value


def _configure_token(token: str, log) -> None:
    _inject_rewrite(
        f"url.https://x-access-token:{token}@github.com/.insteadOf",
        "https://github.com/",
    )
    log("credentials: using GitHub token for https://github.com")


def _configure_ssh(log) -> None:
    _inject_rewrite("url.git@github.com:.insteadOf", "https://github.com/")
    os.environ.setdefault("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=accept-new")
    log("credentials: using forwarded SSH agent for git@github.com")


def configure(log=print) -> bool:
    """Best-effort: make private git clones authenticate in this environment.

    Returns ``True`` when a credential source was configured. On the host the
    system git client already works, so this is a no-op outside the container
    unless a token is explicitly exported.
    """
    token = _token_from_env()
    if token:
        _configure_token(token, log)
        return True

    home = _host_home()
    if home is not None:
        token = _token_from_gh_config(home) or _token_from_credentials(home)
        if token:
            _configure_token(token, log)
            return True
        if _ssh_agent_available():
            _configure_ssh(log)
            return True
        log(
            "credentials: no GitHub token or SSH agent found; private "
            "integrations will fail to clone (set GITHUB_TOKEN/GH_TOKEN)"
        )
    return False
