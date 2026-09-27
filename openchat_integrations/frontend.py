"""Data-driven export of prerendered integration frontend pages.

Replaces the previously hardcoded `frontend/scripts/export_integration_pages.sh`
mapping. The page list comes from the manifest (`frontend.pages`), or from an
integration-owned `integration.frontend.json` when present (Phase 2).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import List, Optional

from .manifest import FrontendPage, Integration, Manifest


class ExportError(RuntimeError):
    pass


def _replace_symlink(target: Path, link: Path) -> None:
    if link.is_symlink() or link.exists():
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def _ensure_frontend_workspace(frontend_root: Path, log) -> None:
    """Add `integrations/*` to the frontend npm workspaces once."""
    package_json = frontend_root / "package.json"
    if not package_json.exists():
        return
    data = json.loads(package_json.read_text(encoding="utf-8"))
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, list):
        return
    if "integrations/*" in workspaces:
        return
    workspaces.append("integrations/*")
    data["workspaces"] = workspaces
    package_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log("frontend: added integrations/* to npm workspaces")


FRONTEND_JSON_NAME = "integration.frontend.json"


def _load_frontend_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def effective_frontend(manifest: Manifest, integ: Integration) -> Optional[Frontend]:
    """Resolve the frontend contribution of an integration.

    An integration owns its frontend contribution: if the integration checkout
    provides `integration.frontend.json`, it fully describes the page set
    (name defaults to the integration id when no manifest entry exists). The
    manifest `frontend` entry only matters for the publish-name/link target
    and a bundled npm `package`, neither of which the JSON needs.
    """
    json_path = manifest.path_for(integ) / FRONTEND_JSON_NAME
    if json_path.exists():
        from .manifest import Frontend as _Frontend

        raw = _load_frontend_json(json_path)
        default = integ.frontend
        name = default.name if default else integ.id
        package = default.package if default else None
        path = default.path if default else "frontend"
        return _Frontend(
            name=str(raw.get("name", name)),
            package=raw.get("package") or package,
            path=str(raw.get("path", path)),
            pages=_pages_from_integration_json(json_path),
            extension=str(raw.get("extension", "") or (default.extension if default else "") or ""),
        )
    return integ.frontend


def link(manifest: Manifest, selected: List[str], log) -> int:
    """Link integration-owned Vike frontend pages/packages into the aggregator.

    Integration pages live in the (possibly private) integration repositories
    under `frontend/pages`. They are symlinked into
    `frontend/pages/integrations/<name>` so Vike's filesystem routing picks
    them up, but only for integrations present in the current profile. This
    keeps per-integration React code out of the public frontend repository.
    Integrations may additionally ship a full npm package under `frontend/`
    (with `package.json`), which is linked into `frontend/integrations/<id>`.
    """
    frontend_root = manifest.repo_root / "frontend"
    if not frontend_root.exists():
        log("frontend: aggregator not present, skipping")
        return 0

    integ_root = frontend_root / "integrations"
    pages_root = frontend_root / "pages" / "integrations"
    packages_linked = 0
    pages_linked = 0
    linked_extensions: List[str] = []
    for integ_id in selected:
        integ = manifest.integrations[integ_id]
        front = effective_frontend(manifest, integ)
        if front is None:
            continue
        src = manifest.path_for(integ) / front.path
        if not src.exists():
            continue
        if front.package and (src / "package.json").exists():
            _replace_symlink(src, integ_root / integ_id)
            packages_linked += 1
        src_pages = src / "pages"
        if src_pages.exists():
            _replace_symlink(src_pages, pages_root / front.name)
            pages_linked += 1
            _anchor_node_modules(src, frontend_root)
            extension = (front.extension or "").strip()
            if extension:
                linked_extensions.append(
                    f'import "../pages/integrations/{front.name}/{extension}";'
                )

    # Always rewrite the extensions entry: profiles without chat extensions
    # must reset the committed baseline to empty, otherwise a previously linked
    # private page set would leave dangling imports.
    write_extensions_entry(frontend_root, linked_extensions)

    if packages_linked:
        _ensure_frontend_workspace(frontend_root, log)
    if packages_linked or pages_linked:
        log(
            f"frontend: linked {pages_linked} page set(s) "
            f"and {packages_linked} package(s)"
        )
    return pages_linked + packages_linked


EXTENSIONS_ENTRY_REL = Path("integrations") / "extensions.gen.ts"


def write_extensions_entry(frontend_root: Path, imports: List[str]) -> None:
    """(Re)generate the aggregator side of integration frontend registrations.

    The public app imports this module at startup (LayoutDefault). It is
    committed as an empty baseline; private integrations only add their import
    when linked (see `integration.frontend.json` -> `extension`).
    """
    entry = frontend_root / EXTENSIONS_ENTRY_REL
    entry.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(imports) + "\n" if imports else ""
    entry.write_text(body, encoding="utf-8")


def _anchor_node_modules(src: Path, frontend_root: Path) -> None:
    """Point the linked page set's `frontend/node_modules` at the aggregator.

    Vite (rolldown) resolves bare imports from the symlink target's real
    location, which is outside the aggregator's node_modules tree. A
    localizing symlink keeps dev-mode and image builds working; the
    integration repos gitignore `frontend/node_modules`. Idempotent.
    """
    node_modules = src / "node_modules"
    rel = os.path.relpath(frontend_root / "node_modules", start=src)
    if node_modules.is_symlink() or node_modules.exists():
        if node_modules.is_symlink() and node_modules.readlink() == Path(rel):
            return
        if not node_modules.is_symlink():
            # A real node_modules in the integration checkout wins.
            return
    try:
        node_modules.symlink_to(rel)
    except OSError:
        log(f"warning: could not create node_modules anchor at {src}")


def _pages_from_integration_json(path: Path) -> List[FrontendPage]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    pages: List[FrontendPage] = []
    for page in raw.get("pages") or []:
        source = str(page.get("source", "")).strip()
        asset = str(page.get("asset", "")).strip()
        if not source or not asset:
            raise ExportError(f"{path}: page needs source and asset")
        pages.append(
            FrontendPage(
                source=source,
                asset=asset,
                route=str(page["route"]) if page.get("route") else None,
                public=bool(page.get("public", False)),
            )
        )
    return pages


def pages_for(manifest: Manifest, integ: Integration) -> List[FrontendPage]:
    front = effective_frontend(manifest, integ)
    if front is None:
        return []
    return front.pages


def export_pages(
    manifest: Manifest,
    selected: List[str],
    dist_dir: Path,
    *,
    check: bool,
    log,
) -> int:
    """Copy prerendered pages into each integration's embedded assets.

    Returns the number of pages exported. When `check` is true, only verifies
    that every source exists.
    """
    count = 0
    missing: List[str] = []
    for integ_id in selected:
        integ = manifest.integrations[integ_id]
        pages = pages_for(manifest, integ)
        if not pages:
            continue
        target_root = manifest.path_for(integ) / "frontend_assets"
        for page in pages:
            src = dist_dir / page.source / "index.html"
            dst = target_root / page.asset
            if not src.exists():
                missing.append(f"{integ_id}: {src}")
                continue
            if not check:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            count += 1

    if missing:
        raise ExportError(
            "missing prerendered pages (run the frontend build first):\n  "
            + "\n  ".join(missing)
        )
    action = "verified" if check else "exported"
    log(f"export: {action} {count} integration pages")
    return count
