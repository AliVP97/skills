#!/usr/bin/env python3
"""Validate every skill under skills/.

Checks frontmatter, name/folder agreement, description rules, a single SKILL.md
per skill, and that every references/ or assets/ path mentioned inside a skill
points at a file that exists. Exits non-zero when anything is wrong.

Usage: python scripts/validate_skills.py   (needs PyYAML: pip install pyyaml)
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"

ALLOWED_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
KEBAB_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
PATH_MENTION = re.compile(r"(?:references|assets)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
SOFT_MAX_LINES = 500


def validate_skill(skill_dir):
    """Return (errors, warnings) for one skill folder."""
    errors, warnings = [], []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"{skill_dir.name}: SKILL.md is missing"], warnings

    nested = sorted(p.relative_to(skill_dir) for p in skill_dir.rglob("SKILL.md") if p != skill_md)
    if nested:
        errors.append(
            f"{skill_dir.name}: only one SKILL.md is allowed per skill, also found: "
            + ", ".join(str(p) for p in nested)
        )

    text = skill_md.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return errors + [f"{skill_dir.name}: SKILL.md has no YAML frontmatter"], warnings

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return errors + [f"{skill_dir.name}: invalid YAML in frontmatter ({exc})"], warnings
    if not isinstance(meta, dict):
        return errors + [f"{skill_dir.name}: frontmatter must be a YAML mapping"], warnings

    unexpected = sorted(set(meta) - ALLOWED_KEYS)
    if unexpected:
        errors.append(f"{skill_dir.name}: unexpected frontmatter key(s): {', '.join(unexpected)}")

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{skill_dir.name}: 'name' is missing")
    else:
        name = name.strip()
        if not KEBAB_CASE.match(name):
            errors.append(f"{skill_dir.name}: name '{name}' must be kebab-case")
        if len(name) > MAX_NAME:
            errors.append(f"{skill_dir.name}: name is longer than {MAX_NAME} characters")
        if name != skill_dir.name:
            errors.append(f"{skill_dir.name}: name '{name}' must match the folder name")

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{skill_dir.name}: 'description' is missing")
    else:
        description = description.strip()
        if len(description) > MAX_DESCRIPTION:
            errors.append(
                f"{skill_dir.name}: description is {len(description)} characters, maximum is {MAX_DESCRIPTION}"
            )
        if "<" in description or ">" in description:
            errors.append(f"{skill_dir.name}: description must not contain angle brackets")

    line_count = text.count("\n") + 1
    if line_count > SOFT_MAX_LINES:
        warnings.append(
            f"{skill_dir.name}: SKILL.md has {line_count} lines; consider moving detail into references/"
        )

    for md_file in sorted(skill_dir.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        for mentioned in sorted(set(PATH_MENTION.findall(content))):
            if not (skill_dir / mentioned).is_file():
                errors.append(
                    f"{skill_dir.name}: {md_file.relative_to(skill_dir)} mentions {mentioned}, which does not exist"
                )

    return errors, warnings


def main():
    if not SKILLS_DIR.is_dir():
        print(f"No skills/ directory found at {SKILLS_DIR}")
        return 1

    skill_dirs = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())
    if not skill_dirs:
        print("skills/ contains no skill folders")
        return 1

    failed = False
    for skill_dir in skill_dirs:
        errors, warnings = validate_skill(skill_dir)
        for message in warnings:
            print(f"warning: {message}")
        for message in errors:
            print(f"error: {message}")
        if errors:
            failed = True
        else:
            print(f"ok: {skill_dir.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
