"""Skills: folders containing SKILL.md (YAML frontmatter + markdown), loaded on demand.

Same progressive-disclosure model as Hermes/Claude skills: the system prompt carries only an index
(name + description); the model calls `skill_view` to read a skill (or one of its linked files).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from .paths import safe_child_name

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Skill:
    name: str
    description: str
    dir: Path

    @property
    def path(self) -> Path:
        return self.dir / "SKILL.md"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def discover(skills_dir: Path) -> list[Skill]:
    out: dict[str, Skill] = {}
    if not skills_dir.is_dir():
        return []
    for f in sorted(skills_dir.rglob("SKILL.md")):
        if any(part.startswith(".") for part in f.relative_to(skills_dir).parts):
            continue
        meta, body = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        name = str(meta.get("name") or f.parent.name).strip()
        desc = str(meta.get("description") or "").strip()
        if not desc:
            first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
            desc = first[:160]
        if meta.get("enabled") is False:
            continue
        out.setdefault(name, Skill(name=name, description=desc, dir=f.parent))
    return sorted(out.values(), key=lambda s: s.name)


def index_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = "\n".join(f"  - {s.name}: {s.description}" for s in skills)
    return (
        "## Skills\n"
        "Before replying, scan the skills below. If one matches or is even partially relevant to the "
        "task, load it with the `skill_view` tool and follow its instructions.\n"
        f"<available_skills>\n{lines}\n</available_skills>")


SKILL_VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": ("Load a skill's SKILL.md, or one of its linked files (references/, templates/, "
                        "scripts/, assets/). The first call returns SKILL.md plus a list of linked files."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from <available_skills>."},
                "file_path": {"type": "string", "description": "Optional path of a linked file inside the skill."},
            },
            "required": ["name"],
        },
    },
}


def skill_view(skills: list[Skill], name: str, file_path: str | None = None) -> str:
    skill = next((s for s in skills if s.name == name), None)
    if skill is None:
        return f"Error: no skill named {name!r}. Available: {', '.join(s.name for s in skills) or 'none'}"
    root = skill.dir.resolve()
    if file_path:
        target = (root / file_path).resolve()
        if root not in target.parents or not target.is_file():
            return f"Error: {file_path!r} is not a file inside skill {name!r}"
        return target.read_text(encoding="utf-8", errors="replace")[:200_000]
    linked = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                    if p.is_file() and p.name != "SKILL.md" and not any(x.startswith(".") for x in p.relative_to(root).parts))
    text = skill.path.read_text(encoding="utf-8", errors="replace")
    if linked:
        text += "\n\n---\nLinked files (load with skill_view(name, file_path)):\n" + "\n".join(f"- {p}" for p in linked)
    return text


# ── management ──
def install(source: str, skills_dir: Path, name: str | None = None, subdir: str | None = None,
            force: bool = False) -> Path:
    """Install from a local folder (containing SKILL.md) or a git URL (optionally a subdir of the repo)."""
    tmp: tempfile.TemporaryDirectory | None = None
    try:
        src = Path(source).expanduser()
        if not src.exists():
            if not re.match(r"^(https?://|git@|ssh://)", source):
                raise FileNotFoundError(f"{source} is neither a folder nor a git URL")
            tmp = tempfile.TemporaryDirectory(prefix="aimpoint-skill-")
            subprocess.run(["git", "clone", "--depth", "1", source, tmp.name], check=True,
                           capture_output=True, text=True)
            src = Path(tmp.name)
        if subdir:
            src = src / subdir
        if src.is_file() and src.name == "SKILL.md":
            src = src.parent
        if not (src / "SKILL.md").is_file():
            found = sorted(src.rglob("SKILL.md"))
            if len(found) == 1:
                src = found[0].parent
            else:
                raise FileNotFoundError(
                    f"No single SKILL.md at {src} ({len(found)} found); pass --subdir to pick one")
        meta, _ = parse_frontmatter((src / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        final = safe_child_name(name or str(meta.get("name") or src.name), "skill name")
        dest = skills_dir / final
        if dest.exists():
            if not force:
                raise FileExistsError(f"Skill {final!r} already exists (use --force to replace)")
            shutil.rmtree(dest)
        skills_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
        return dest
    finally:
        if tmp:
            tmp.cleanup()


def scaffold(skills_dir: Path, name: str, description: str) -> Path:
    n = safe_child_name(name, "skill name")
    d = skills_dir / n
    if d.exists():
        raise FileExistsError(f"Skill {n!r} already exists")
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {n}\ndescription: {description}\n---\n\n# {n}\n\n## When to Use\n\n## Procedure\n\n",
        encoding="utf-8")
    return d
