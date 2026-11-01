"""Load a skill's agent-facing ``SKILL.md`` instruction document.

Convention: a skill's Markdown instructions live in a directory named after
the skill, next to this package::

    app/agents/skills/consolidation/SKILL.md

The directory intentionally carries **no** ``__init__.py``: the sibling
module ``consolidation.py`` therefore remains the importable implementation,
and the directory exists only to hold the instruction document. This keeps
the Python skill responsible for execution while the Markdown describes when
and how the agent should use it.

This is deliberately tiny — a name→path resolver and a reader — not a
skill-loading framework. Deterministic accounting logic never lives in the
Markdown.
"""

from __future__ import annotations

from pathlib import Path

#: The instruction file name every skill directory uses.
SKILL_FILE_NAME = "SKILL.md"

#: The directory holding this helper; skill folders are siblings of it.
SKILLS_ROOT = Path(__file__).resolve().parent


class SkillInstructionsNotFound(FileNotFoundError):
    """No ``SKILL.md`` exists for the requested skill."""


def skill_instructions_path(skill_name: str) -> Path:
    """Return the ``SKILL.md`` path for ``skill_name`` (existence not checked)."""
    name = (skill_name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"invalid skill name: {skill_name!r}")
    return SKILLS_ROOT / name / SKILL_FILE_NAME


def load_skill_instructions(skill_name: str) -> str:
    """Read one skill's ``SKILL.md`` text.

    Raises:
        SkillInstructionsNotFound: When the skill has no instruction file.
    """
    path = skill_instructions_path(skill_name)
    if not path.is_file():
        raise SkillInstructionsNotFound(
            f"no {SKILL_FILE_NAME} for skill '{skill_name}' at {path}"
        )
    return path.read_text(encoding="utf-8")


def has_skill_instructions(skill_name: str) -> bool:
    """Whether a ``SKILL.md`` exists for ``skill_name``."""
    try:
        return skill_instructions_path(skill_name).is_file()
    except ValueError:
        return False
