# Changelog

## 0.2.0 - 2026-10-16

- New skill `project-estimation-assistant`:
  - solution comparison in three levels with a research protocol and a dated `facts.md`
  - domestic-first choices and national-internet checks
  - three-point estimates with an 80% target and a buffer floor
  - roles, rates and team-size scenarios against the client's deadline
  - operating costs with break-even, support plan and Jalali schedule
  - change-request pricing, paid discovery and calibration from the outcome log
  - `scripts/estimate.py` does all arithmetic and dates
- `software-proposal-assistant` and the new skill now work as one system:
  - shared contract (`assets/shared-contract.md`) for file paths, settings, the estimate package and hand-off points
  - step 3 hands the estimate to the new skill, with the manual table kept as fallback
  - steps 1, 2, 4, 5, 7 and 8 use the estimate package
- Personal files moved to `~/.config/freelance-skills/`, and project files to `docs/proposal/{client,internal}/` with `internal/` git-ignored. Google Drive support removed.
- Settings split into shared, proposal and estimation sections. The outcome log template moved into the shared contract and gained a per-milestone table.
- The validator now checks `scripts/` paths, compiles bundled scripts and requires identical shared-contract copies.

## 0.1.0 - 2026-10-14

- Initial release of `software-proposal-assistant`: screening, discovery, estimate sheet, proposal writing, legal review, simulated client, final check, negotiation and outcome log.
- Repo layout for `npx skills add`, validator script and CI workflow.
