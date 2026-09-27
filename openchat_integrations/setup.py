"""Profile setup: materialize profile-scoped repositories and links.

The integration manager keeps the public repository free of private source.
Repositories that are only needed for a given profile (the private CI tooling,
Helm chart, mobile client, ...) are declared in the public ``profile_setup.yaml``
and materialized on demand by::

    openchat-integrations setup --profile full

Every other command runs this step automatically when the profile's setup
marker is missing or stale, so a build never runs against a half-configured
workspace.

The private integrations manifest and its lockfile live in the private ``ci``
repository. Profiles that only need those two files (``default``, ``full``,
``full-android``) fetch them with a sparse checkout of ``openchat/`` and mirror
them into ``.integrations/private/``; the ``full-ci`` profile additionally
materializes the complete ``ci`` tooling and the Helm chart.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .manifest import (
    MANIFEST_NAME,
    PRIVATE_LOCK_REL,
    PRIVATE_MANIFEST_REL,
    _ensure_yaml,
)

yaml = _ensure_yaml()

SETUP_SPEC_NAME = "profile_setup.yaml"
SETUP_DIR_REL = Path(".integrations") / ".setup"
SETUP_MARKER_VERSION = 2

# Mirrored private manifest/lock, copied out of a ci checkout (full or sparse)
# so the manager can read them from one stable, gitignored location.
FRAGMENT_DIR_REL = Path(".integrations") / "private"
FRAGMENT_MANIFEST_NAME = "integrations.private.yaml"
FRAGMENT_LOCK_NAME = "integrations.private.lock.json"

# Candidate locations of the `openchat/` fragment inside a ci checkout.
FRAGMENT_SOURCE_RELS = (
    Path("development") / "ci" / "openchat",
    Path(".integrations") / "ci-fragment" / "openchat",
)


class SetupError(RuntimeError):
    pass


@dataclass
class SetupRepo:
    id: str
    repo: str
    path: str
    ref: str = "main"
    sparse: List[str] = field(default_factory=list)
    fragment: bool = False


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
                sparse=[str(s) for s in (entry.get("sparse") or [])],
                fragment=bool(entry.get("fragment", False)),
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
    """Stable, gitignored location the manager reads the private fragment from."""
    return repo_root / PRIVATE_MANIFEST_REL


def private_lock_path(repo_root: Path) -> Path:
    return repo_root / PRIVATE_LOCK_REL


def existing_private_lock(repo_root: Path) -> Optional[Path]:
    """The private lockfile to read, preferring the mirror over the ci checkout."""
    for rel in (PRIVATE_LOCK_REL, Path("development/ci/openchat/integrations.private.lock.json")):
        candidate = repo_root / rel
        if candidate.exists():
            return candidate
    return None


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


def _apply_sparse(path: Path, sparse: List[str]) -> None:
    if sparse:
        _run_git(["sparse-checkout", "init", "--cone"], cwd=path)
        _run_git(["sparse-checkout", "set", *sparse], cwd=path)
        return
    # Disable a previously applied sparse checkout so a full profile sees the
    # complete repository (e.g. `ci` after `ci_fragment`).
    try:
        _run_git(["sparse-checkout", "disable"], cwd=path)
    except SetupError:
        pass


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
        _apply_sparse(path, repo.sparse)
        return _rev_parse(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"setup: cloning {repo.id} -> {repo.path}")
    if repo.sparse:
        _run_git(["clone", "--filter=blob:none", "--no-checkout", repo.repo, str(path)])
        _run_git(["checkout", "--force", repo.ref], cwd=path)
        _apply_sparse(path, repo.sparse)
    else:
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


def _find_fragment_source(repo_root: Path) -> Optional[Path]:
    for rel in FRAGMENT_SOURCE_RELS:
        candidate = repo_root / rel
        if (candidate / FRAGMENT_MANIFEST_NAME).exists():
            return candidate
    return None


def _mirror_fragment(repo_root: Path, log) -> bool:
    """Copy the private fragment/lock into `.integrations/private/` if present."""
    source = _find_fragment_source(repo_root)
    if source is None:
        return False
    dest = repo_root / FRAGMENT_DIR_REL
    dest.mkdir(parents=True, exist_ok=True)
    changed = False
    for name in (FRAGMENT_MANIFEST_NAME, FRAGMENT_LOCK_NAME):
        src = source / name
        if not src.exists():
            continue
        dst = dest / name
        content = src.read_bytes()
        if not dst.exists() or dst.read_bytes() != content:
            dst.write_bytes(content)
            changed = True
    log(f"setup: mirrored private fragment from {source.relative_to(repo_root)}")
    return changed


def _fragment_digest(repo_root: Path) -> str:
    source = _find_fragment_source(repo_root)
    if source is None:
        return ""
    manifest = source / FRAGMENT_MANIFEST_NAME
    return hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else ""


def _digest(
    spec: SetupSpec, profile: str, repos: List[SetupRepo], fragment: str
) -> str:
    profile_setup = spec.profile(profile)
    payload = {
        "version": SETUP_MARKER_VERSION,
        "profile": profile,
        "repos": [
            {
                "id": r.id,
                "repo": r.repo,
                "path": r.path,
                "ref": r.ref,
                "sparse": r.sparse,
            }
            for r in repos
        ],
        "symlinks": [
            {"link": l.link, "target": l.target} for l in profile_setup.symlinks
        ],
        "fragment": fragment,
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
    only_repo: Optional[str] = None,
    log=print,
) -> bool:
    """Materialize a profile's setup. Returns True when work was performed."""
    repo_root = repo_root.resolve()
    spec = SetupSpec.load(repo_root)

    if only_repo:
        if only_repo not in spec.repos:
            raise SetupError(f"unknown setup repo {only_repo!r}")
        resolved = _resolve_repo(repo_root, spec.repos[only_repo])
        _ensure_repo(resolved, update=True, log=log)
        return True

    repos = spec.repos_for(profile)
    profile_setup = spec.profile(profile)
    wants_fragment = any(r.fragment for r in repos)
    fragment = _fragment_digest(repo_root) if wants_fragment else ""
    digest = _digest(spec, profile, repos, fragment)

    if not force:
        marker = _read_marker(repo_root, profile)
        if marker and marker.get("digest") == digest:
            missing = [r for r in repos if not (repo_root / r.path).exists()]
            if not missing:
                if wants_fragment:
                    _mirror_fragment(repo_root, lambda _m: None)
                return False

    if not repos and not profile_setup.symlinks and not wants_fragment:
        _write_marker(repo_root, profile, digest, [])
        return False

    recorded = []
    for repo in repos:
        resolved = _resolve_repo(repo_root, repo)
        commit = _ensure_repo(resolved, update=update, log=log)
        recorded.append({"id": repo.id, "path": repo.path, "commit": commit})

    for link in profile_setup.symlinks:
        _apply_link(repo_root, link)
        log(f"setup: linked {link.link} -> {link.target}")

    if wants_fragment:
        _mirror_fragment(repo_root, log)
    _write_marker(repo_root, profile, digest, recorded)
    log(f"setup: profile={profile} ready")
    return True


def _resolve_repo(repo_root: Path, repo: SetupRepo) -> SetupRepo:
    return SetupRepo(
        id=repo.id,
        repo=repo.repo,
        path=str((repo_root / repo.path).resolve()),
        ref=repo.ref,
        sparse=list(repo.sparse),
        fragment=repo.fragment,
    )


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


def canonical_lock_path(repo_root: Path) -> Optional[Path]:
    """The private lockfile inside a full ci checkout, when present."""
    source = repo_root / FRAGMENT_SOURCE_RELS[0]
    if source.exists():
        return source / FRAGMENT_LOCK_NAME
    return None


def canonical_manifest_path(repo_root: Path) -> Optional[Path]:
    source = repo_root / FRAGMENT_SOURCE_RELS[0]
    if source.exists():
        return source / FRAGMENT_MANIFEST_NAME
    return None
