"""Shared skill library + per-project assignment (owner picks which skill goes to which project).

Library: <CCFLEET_HOME>/skills-lib/<name>/ — copied from every source (user-level ~/.claude/skills,
each account's config dir, every known project's .claude/skills). A skill = a dir with SKILL.md.
Name clash → the copy whose SKILL.md is newest wins. Secrets/junk are never copied (.env*, node_modules).

Assignment (config "skill_assign": {"<project dir>": ["skill", ...]}) → a directory junction
<project>/.claude/skills/<name> → library, plus a .git/info/exclude entry so the link never gets
committed to the project's repo. Native (real) skill dirs in a project are never touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import ccfleet
import fleet_accounts

LIB = ccfleet.HOME / "skills-lib"
SKIP = shutil.ignore_patterns(".env", ".env.*", "node_modules", "__pycache__", ".git")
PROJECTS_ROOT = Path(os.environ.get("CCFLEET_PROJECTS_ROOT", Path.home() / "Projects"))


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def _skill_dirs(root: Path):
    if not root.is_dir():
        return
    for d in root.iterdir():
        if d.is_dir() and not _is_link(d) and (d / "SKILL.md").is_file():
            yield d


def project_dirs(cfg: dict) -> list[Path]:
    """Known projects: ccfleet sessions + Claude history + folders under ~/Projects with .claude/ or .git/."""
    seen: dict[str, Path] = {}
    for s in cfg["sessions"]:
        if s.get("cwd"):
            seen[os.path.normcase(s["cwd"])] = Path(s["cwd"])
    for p in ccfleet.discover_projects(cfg).values():
        if Path(p["cwd"]).is_dir():
            seen.setdefault(os.path.normcase(p["cwd"]), Path(p["cwd"]))
    if PROJECTS_ROOT.is_dir():
        for d in PROJECTS_ROOT.iterdir():
            if d.is_dir() and ((d / ".claude").is_dir() or (d / ".git").is_dir()):
                seen.setdefault(os.path.normcase(str(d)), d)
    return sorted(seen.values(), key=lambda p: str(p).lower())


def description(skill_dir: Path) -> str:
    try:
        for line in (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace").splitlines()[:15]:
            if line.lower().startswith("description:"):
                return line.split(":", 1)[1].strip().strip('"')[:300]
    except OSError:
        pass
    return ""


def import_all(cfg: dict) -> dict:
    """Refresh the library from every source. Returns {name: source_dir}."""
    LIB.mkdir(parents=True, exist_ok=True)
    sources = [Path.home() / ".claude" / "skills"]
    sources += [fleet_accounts.config_dir_of(a) / "skills" for a in cfg["accounts"].values()]
    sources += [p / ".claude" / "skills" for p in project_dirs(cfg)]
    best: dict[str, Path] = {}
    for root in sources:
        for d in _skill_dirs(root):
            cur = best.get(d.name)
            if cur is None or (d / "SKILL.md").stat().st_mtime > (cur / "SKILL.md").stat().st_mtime:
                best[d.name] = d
    for name, src in best.items():
        dst = LIB / name
        if dst.exists() and (dst / "SKILL.md").stat().st_mtime >= (src / "SKILL.md").stat().st_mtime:
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=SKIP)
        (dst / ".ccfleet-source").write_text(str(src), encoding="utf-8")
    ccfleet.log(f"skills: library has {len(list(_skill_dirs(LIB)))} skills from {len(sources)} sources")
    return {n: str(p) for n, p in best.items()}


def library() -> list[dict]:
    out = []
    for d in sorted(_skill_dirs(LIB), key=lambda p: p.name.lower()):
        src = (d / ".ccfleet-source").read_text(encoding="utf-8") if (d / ".ccfleet-source").exists() else ""
        out.append({"name": d.name, "description": description(d), "source": src})
    return out


def project_view(cfg: dict, project: Path) -> dict:
    sk = project / ".claude" / "skills"
    native, linked = [], []
    if sk.is_dir():
        for d in sk.iterdir():
            if _is_link(d):
                linked.append(d.name)
            elif d.is_dir() and (d / "SKILL.md").is_file():
                native.append(d.name)
    assigned = cfg.get("skill_assign", {}).get(os.path.normcase(str(project)), [])
    return {"cwd": str(project), "native": sorted(native), "linked": sorted(linked), "assigned": sorted(assigned)}


def _exclude(project: Path, names: list[str]) -> None:
    ex = project / ".git" / "info" / "exclude"
    if not ex.parent.is_dir():
        return
    lines = ex.read_text(encoding="utf-8").splitlines() if ex.exists() else []
    # gitignore has no trailing comments → keep our entries inside a marked block
    keep, inside = [], False
    for l in lines:
        if l == "# ccfleet:begin":
            inside = True
        elif l == "# ccfleet:end":
            inside = False
        elif not inside:
            keep.append(l)
    if names:
        keep += ["# ccfleet:begin"] + [f"/.claude/skills/{n}/" for n in names] + ["# ccfleet:end"]
    ex.write_text("\n".join(keep) + "\n", encoding="utf-8")


def assign(cfg: dict, project: Path, names: list[str]) -> dict:
    """Make <project>/.claude/skills contain exactly the assigned library skills as junctions."""
    lib_names = {d.name for d in _skill_dirs(LIB)}
    names = sorted(n for n in set(names) if n in lib_names)
    sk = project / ".claude" / "skills"
    sk.mkdir(parents=True, exist_ok=True)
    for d in list(sk.iterdir()):  # drop our links that are no longer wanted
        if _is_link(d) and d.name not in names and str(LIB).lower() in os.path.realpath(d).lower():
            os.rmdir(d)  # removes the junction only, never the library content
    for n in names:
        link = sk / n
        if link.exists() or _is_link(link):
            continue  # native skill with the same name wins; existing link kept
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(LIB / n)], capture_output=True)
    _exclude(project, names)
    cfg.setdefault("skill_assign", {})[os.path.normcase(str(project))] = names
    ccfleet.save_json(ccfleet.CONFIG, cfg)
    ccfleet.log(f"skills: {project} <- {len(names)} shared skills")
    return project_view(cfg, project)
