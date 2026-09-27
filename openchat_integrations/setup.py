"""Profile setup: materialize profile-scoped repositories and links.

The integration manager keeps the public repository free of private source.
Repositories that are only needed for a given profile (e.g. the private CI,
Helm, mobile and LLM-context checkouts) are declared in the public
``profile_setup.yaml`` and materialized on demand by::

    openchat-integrations setup --profile full

Every other command runs this step automatically when the profile's setup
marker is missing or stale, so a build never runs against a half-configured
workspace. The public spec only ever names repository *locations*; all private
source (the private integrations manifest and its lockfile) lives inside the
``ci`` checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .manifest import (
    MANIFEST_NAME,
    PRIVATE_LOCK_REL,
    PRIVATE_MANIFEST_REL,
    ManifestError,
    _ensure_yaml,
)

yaml = _ensure_yaml()

SETUP_SPEC_NAME = "profile_setup.yaml"
SETUP_DIR_REL = Path(".integrations") / ".setup"
SETUP_MARKER_VERSION = 1

VALID_PROFILE_KEYS = ("repos", "symlinks")


class SetupError(RuntimeError):
    pass


@dataclass
class SetupRepo:
    id: str
    repo: str
    path: str
    ref: str = "main"


@dataclass
class SetupLink:
    link: str
    target: str


@dataclass
class ProfileSetup:
    repos: List[str] = field(default_factory=list)
    symlinks: List[SetupLink] = field(default_factory=list)


@dataclass
class SetupSpec:
    version: int
    repos: Dict[str, SetupRepo]
    profiles: Dict[str, ProfileSetup]
    repo_root: Path

    @classmethod
    def load(cls, repo_root: Path) -> "SetupSpec":
        repo_root = repo_root.resolve()
        path = repo_root / SETUP_SPEC_NAME
        if not path.exists():
            return cls(version=1, repos={}, profiles={}, repo_root=repo_root)

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise SetupError(f"{SETUP_SPEC_NAME} root must be a mapping")

        repos: Dict[str, SetupRepo] = {}
        for repo_id, entry in (raw.get("repos") or {}).items():
            if not isinstance(entry, dict):
                raise SetupError(f"setup repo {repo_id!r} must be a mapping")
            repo = str(entry.get("repo", "")).strip()
            repo_path = str(entry.get("path", "")).strip()
            if not repo or not repo_path:
                raise SetupError(f"setup repo {repo_id!r} needs repo and path")
            repos[str(repo_id)] = SetupRepo(
                id=str(repo_id),
                repo=repo,
                path=repo_path,
                ref=str(entry.get("ref", "main")).strip() or "main",
            )

        profiles: Dict[str, ProfileSetup] = {}
        for profile, entry in (raw.get("profiles") or {}).items():
            if entry is None:
                entry = {}
            if not isinstance(entry, dict):
                raise SetupError(f"setup profile {profile!r} must be a mapping")
            ids = [str(r) for r in (entry.get("repos") or [])]
            for repo_id in ids:
                if repo_id not in repos:
                    raise SetupError(
                        f"setup profile {profile!r} references unknown repo {repo_id!r}"
                    )
            links: List[SetupLink] = []
            for link in entry.get("symlinks") or []:
                if not isinstance(link, dict):
                    raise SetupError(f"symlink in profile {profile!r} must be a mapping")
                link_path = str(link.get("link", "")).strip()
                target = str(link.get("target", "")).strip()
                if not link_path or not target:
                    raise SetupError(
                        f"symlink in profile {profile!r} needs link and target"
                    )
                links.append(SetupLink(link=link_path, target=target))
            profiles[str(profile)] = ProfileSetup(repos=ids, symlinks=links)

        return cls(
            version=int(raw.get("version", 1)),
            repos=repos,
            profiles=profiles,
            repo_root=repo_root,
        )

    def profile(self, name: str) -> ProfileSetup:
        return self.profiles.get(name, ProfileSetup())

    def repos_for(self, name: str) -> List[SetupRepo]:
        return [self.repos[r] for r in self.profile(name).repos]


def default_profile(repo_root: Path) -> str:
    manifest_path = repo_root / MANIFEST_NAME
    if not manifest_path.exists():
        return "core-only"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict):
        return str(raw.get("default_profile", "core-only"))
    return "core-only"


def resolve_profile(repo_root: Path, requested: Optional[str]) -> str:
    """Resolve the profile name *without* requiring the (possibly private) manifest."""
    if requested:
        return requested
    return os.environ.get("INTEGRATION_PROFILE") or default_profile(repo_root)


def private_manifest_path(repo_root: Path) -> Path:
    return repo_root / PRIVATE_MANIFEST_REL


def private_lock_path(repo_root: Path) -> Path:
    return repo_root / PRIVATE_LOCK_REL


def _run_git(args, cwd: Optional[Path] = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        cmd = "git " + " ".join(args)
        raise SetupError(f"{cmd} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _rev_parse(path: Path) -> str:
    try:
        return _run_git(["rev-parse", "HEAD"], cwd=path)
    except SetupError:
        return ""


def _ensure_repo(repo: SetupRepo, *, update: bool, log) -> str:
    path = Path(repo.path)
    if not path.is_absolute():
        raise SetupError(f"setup repo {repo.id!r} path must be absolute after resolution")
    if (path / ".git").exists():
        if update:
            log(f"setup: fetch {repo.id} ({repo.path})")
            _run_git(["fetch", "--tags", "--force", "origin"], cwd=path)
            _run_git(["checkout", "--force", repo.ref], cwd=path)
        else:
            log(f"setup: using existing {repo.id} ({repo.path})")
        return _rev_parse(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"setup: cloning {repo.id} -> {repo.path}")
    _run_git(["clone", "--filter=blob:none", "--no-checkout", repo.repo, str(path)])
    _run_git(["fetch", "--tags", "--force", "origin"], cwd=path)
    _run_git(["checkout", "--force", repo.ref], cwd=path)
    return _rev_parse(path)


def _apply_link(repo_root: Path, link: SetupLink) -> None:
    link_path = repo_root / link.link
    if link_path.is_symlink():
        if os.readlink(link_path) == link.target:
            return
        link_path.unlink()
    elif link_path.exists():
        raise SetupError(
            f"cannot create symlink {link.link!r}: path exists and is not a symlink"
        )
    link_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(link.target, link_path)


def _digest(spec: SetupSpec, profile: str, repos: List[SetupRepo], extra: str) -> str:
    profile_setup = spec.profile(profile)
    payload = {
        "version": SETUP_MARKER_VERSION,
        "profile": profile,
        "repos": [
            {"id": r.id, "repo": r.repo, "path": r.path, "ref": r.ref} for r in repos
        ],
        "symlinks": [
            {"link": l.link, "target": l.target} for l in profile_setup.symlinks
        ],
        "extra": extra,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _marker_path(repo_root: Path, profile: str) -> Path:
    return repo_root / SETUP_DIR_REL / f"{profile}.json"


def _read_marker(repo_root: Path, profile: str) -> Optional[dict]:
    path = _marker_path(repo_root, profile)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def ensure(
    repo_root: Path,
    profile: str,
    *,
    force: bool = False,
    update: bool = False,
    log=print,
) -> bool:
    """Materialize a profile's setup. Returns True when work was performed."""
    repo_root = repo_root.resolve()
    spec = SetupSpec.load(repo_root)
    repos = spec.repos_for(profile)
    profile_setup = spec.profile(profile)

    # Hash the private fragment too: refreshing it must re-run setup.
    fragment = private_manifest_path(repo_root)
    extra = ""
    if fragment.exists():
        extra = hashlib.sha256(fragment.read_bytes()).hexdigest()

    digest = _digest(spec, profile, repos, extra)

    if not force:
        marker = _read_marker(repo_root, profile)
        if marker and marker.get("digest") == digest:
            missing = [r for r in repos if not (repo_root / r.path).exists()]
            if not missing:
                return False

    if not repos and not profile_setup.symlinks and not fragment.exists():
        # Nothing to materialize for this profile.
        _write_marker(repo_root, profile, digest, [])
        return False

    recorded = []
    for repo in repos:
        resolved = SetupRepo(
            id=repo.id,
            repo=repo.repo,
            path=str((repo_root / repo.path).resolve()),
            ref=repo.ref,
        )
        commit = _ensure_repo(resolved, update=update, log=log)
        recorded.append({"id": repo.id, "path": repo.path, "commit": commit})

    for link in profile_setup.symlinks:
        _apply_link(repo_root, link)
        log(f"setup: linked {link.link} -> {link.target}")

    _write_marker(repo_root, profile, digest, recorded)
    log(f"setup: profile={profile} ready")
    return True


def _write_marker(
    repo_root: Path, profile: str, digest: str, repos: List[dict]
) -> None:
    path = _marker_path(repo_root, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SETUP_MARKER_VERSION,
        "profile": profile,
        "digest": digest,
        "repos": repos,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
