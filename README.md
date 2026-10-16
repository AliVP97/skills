# skills

Agent skills for freelance developers who take on projects from Iranian clients. They install with the [`skills`](https://skills.sh) CLI (`npx skills add`), so the same folders work in Claude Code and in other agents that read `SKILL.md` skills.

> مجموعه‌ای از Skillهای فارسی برای توسعه‌دهندگان فریلنسر؛ از اولین پیام کارفرما تا برآورد، پروپوزال، ارزیابی حقوقی و مذاکره.

## Skills

The two skills work as one system. The proposal assistant runs the process and hands step 3 (the estimate) to the estimation assistant. Each skill still works on its own: without the estimation assistant, step 3 falls back to a manual estimate table, and the estimation assistant answers stand-alone questions such as "how long would this take and what would it need?".

| Skill | What it does |
| --- | --- |
| [`software-proposal-assistant`](skills/software-proposal-assistant) | Takes a vague project offer through eight steps: screening (go / go with conditions / decline), discovery, estimate and price sheet, proposal writing, risk and legal review against Iranian practice, a simulated-client critique, a pass/fail check before sending, and negotiation with an outcome log. Works in Persian and writes milestone-priced proposals in Toman. |
| [`project-estimation-assistant`](skills/project-estimation-assistant) | Researches and compares solutions (custom build, ready-made, low-code; domestic services first, national-internet resilience), estimates effort with three-point estimates and an 80% target, plans roles and team size against the client's deadline, prices hosting, services and post-handover support, builds a Jalali schedule, and prices change requests. A bundled script does all arithmetic and dates. |

## Install

```bash
# every skill in this repo (recommended: the two skills work together)
npx skills add AliVP97/skills

# just one skill
npx skills add AliVP97/skills --skill project-estimation-assistant

# Claude Code only, user-level (available in every project)
npx skills add AliVP97/skills --agent claude-code --global
```

You can also pass the full URL: `npx skills add https://github.com/AliVP97/skills`. `npx skills add --help` lists all options and `npx skills update` pulls newer versions.

The estimation assistant runs `scripts/estimate.py`, which needs Python 3.9+ and `jdatetime` (`pip install jdatetime`).

## Before first use

Your personal data never lives in this repo. The skills keep it in two places:

- **A personal folder shared by all projects:** `~/.config/freelance-skills/`, holding `settings.md` (hourly rate, expertise, free hours, red lines, team members, estimation defaults), `facts.md` (researched prices and lead times with source and expiry date), `outcome-log.md` (proposal outcomes and actual hours per milestone) and `legal-clauses.md` (your approved clauses). On first run the assistant asks only for what it needs, shows you the file and writes it after you approve. Keep this folder outside any git repo.
- **A folder per project:** `docs/proposal/` inside the project folder (usually the project's own repo). `client/` holds what the client sees; `internal/` holds screening, estimates, price sheets and reviews, and is added to the project's `.gitignore` from day one, because the repo is handed to the client at the end and git history keeps whatever was committed.

The layout of these files is defined once in `assets/shared-contract.md`, which both skills ship.

## Repo layout

```
skills/
  software-proposal-assistant/
    SKILL.md           entry point: principles, step order, routing
    references/        one file per step, read only when that step runs
    assets/            templates: proposal, manual estimate table, settings section, clauses, shared contract
  project-estimation-assistant/
    SKILL.md           entry point: modes, phase order, checkpoints, principles
    references/        one file per phase
    assets/            shared contract, settings section, estimate JSON and package templates, checklists
    scripts/estimate.py
scripts/validate_skills.py
CHANGELOG.md
```

`npx skills add` discovers every `skills/<name>/SKILL.md`, so adding a skill means adding a folder there. The `name` in its frontmatter must match the folder name.

## Improving a skill

Clone the repo first: `git clone https://github.com/AliVP97/skills.git`.

1. **Edit** the files under `skills/<name>/`. Keep `SKILL.md` short (under about 500 lines) and put per-step detail in `references/`. When you add a rule, write down why it matters; a rule with a reason generalises to cases the example did not cover.
2. **Shared contract:** `assets/shared-contract.md` exists in both skills and must stay byte-identical, because each skill is installed on its own and cannot read the other's files. Edit both copies together.
3. **Validate:** `pip install pyyaml && python3 scripts/validate_skills.py`. It checks frontmatter (a stray colon in `description` silently breaks a skill), name and folder agreement, description length, that every `references/`, `assets/` and `scripts/` path mentioned in a skill exists, that bundled scripts compile, and that the shared contract copies match.
4. **Try it** in a throwaway project, not inside this repo (the CLI also scans `.claude/skills/`, so a test copy inside the repo shows up as a duplicate):
   ```bash
   mkdir /tmp/try-skills && cd /tmp/try-skills
   npx skills add /path/to/skills --agent claude-code --copy -y
   ```
   Re-run the same command after each edit to refresh the copy.
5. **Test on a real proposal.** When the output is wrong, fix the instruction in the step file where the behaviour comes from, not in `SKILL.md`.
6. **Release:** add a line to `CHANGELOG.md`, commit, tag (`git tag v0.2.0 && git push --tags`) and push. Users get the change with `npx skills update`.

## Disclaimer

The legal review in `software-proposal-assistant` is a drafting and checking aid, not legal advice. Anything it flags as needing a lawyer needs a lawyer, and legal claims without a source are labelled unverified.

The estimates in `project-estimation-assistant` are planning aids, not guarantees. Service prices, lead times, market rates and sanctions change; the assistant attaches a source and an expiry date to each of them and labels anything it could not source as unverified.
