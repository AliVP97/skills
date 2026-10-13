# skills

Agent skills for freelance developers who take on projects from Iranian clients. They install with the [`skills`](https://skills.sh) CLI (`npx skills add`), so the same folders work in Claude Code and in other agents that read `SKILL.md` skills.

> مجموعه‌ای از Skillهای فارسی برای توسعه‌دهندگان فریلنسر؛ از اولین پیام کارفرما تا پروپوزال، ارزیابی حقوقی و مذاکره.

## Skills

| Skill | What it does |
|-------|--------------|
| [`software-proposal-assistant`](skills/software-proposal-assistant) | Takes a vague project offer through eight steps: screening (go / go with conditions / decline), discovery, an estimate sheet, proposal writing, risk and legal review against Iranian practice, a simulated-client critique, a pass/fail check before sending, and negotiation with an outcome log. Works in Persian and writes milestone-priced proposals in Toman. |

## Install

```bash
# every skill in this repo
npx skills add OWNER/skills

# just one skill
npx skills add OWNER/skills --skill software-proposal-assistant

# Claude Code only, user-level (available in every project)
npx skills add OWNER/skills --skill software-proposal-assistant --agent claude-code --global
```

Replace `OWNER` with the GitHub account that hosts this repo. `npx skills add --help` lists all options and `npx skills update` pulls newer versions.

## Before first use

The proposal assistant reads your personal settings (hourly rate, expertise, red lines, tone) from a file you own. Start from `skills/software-proposal-assistant/assets/settings-template.md`, fill it in, and keep the result **outside this repo**, for example in a private Google Drive folder. Do the same for your approved legal clauses and your outcome log. This repo should only ever hold templates, never client names, rates or real proposals.

## Repo layout

```
skills/
  software-proposal-assistant/
    SKILL.md           entry point: principles, step order, routing
    references/        one file per step, read only when that step runs
    assets/            templates: proposal, estimate table, settings, clauses, outcome log
scripts/validate_skills.py
.github/workflows/validate.yml
CHANGELOG.md
```

`npx skills add` discovers every `skills/<name>/SKILL.md`, so adding a skill means adding a folder there. The `name` in its frontmatter must match the folder name.

## Improving a skill

1. **Edit** the files under `skills/<name>/`. Keep `SKILL.md` short (under about 500 lines) and put per-step detail in `references/`. When you add a rule, write down why it matters; a rule with a reason generalises to cases the example did not cover.
2. **Validate:** `pip install pyyaml && python scripts/validate_skills.py`. It checks frontmatter (a stray colon in `description` silently breaks a skill), name and folder agreement, description length, and that every `references/` and `assets/` path mentioned in the skill exists.
3. **Try it** in a throwaway project, not inside this repo (the CLI also scans `.claude/skills/`, so a test copy inside the repo shows up as a duplicate):
   ```bash
   mkdir /tmp/try-skills && cd /tmp/try-skills
   npx skills add /path/to/skills --skill software-proposal-assistant --agent claude-code --copy -y
   ```
   Re-run the same command after each edit to refresh the copy.
4. **Test on a real proposal.** When the output is wrong, fix the instruction in the step file where the behaviour comes from, not in `SKILL.md`.
5. **Release:** add a line to `CHANGELOG.md`, commit, tag (`git tag v0.2.0 && git push --tags`) and push. Users get the change with `npx skills update`.

## Roadmap

- `project-estimation-assistant`: compares implementation options and their costs and replaces the manual estimate sheet in step 3 of the proposal assistant. The estimate table is the fixed interface between them.

## Disclaimer

The legal review in `software-proposal-assistant` is a drafting and checking aid, not legal advice. Anything it flags as needing a lawyer needs a lawyer, and legal claims without a source are labelled unverified.
