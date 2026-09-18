# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project Overview

Serverless Terraform control plane for WebbPulse, replacing HCP Terraform. See
`TFC-REPLACEMENT.md` in the WebbPulse root for the replacement plan and current
status. This repository is newly created and carries only the
project-agnostic rules below; commands, architecture and deployment sections
arrive with the code that needs them.

## Branching and deploys

```
feature/* ──PR──▶ staging ──PR──▶ main
                     │              │
                     ▼              ▼
           AWS 870550636948   AWS 897427573432
              (staging)          (production)
```

- Branch new work from `staging`, not `main`. PR into `staging`. Releasing is a
  PR from `staging` into `main`; that PR is the release boundary.
- Never commit directly to `main` or `staging`. Never force-push either. Stacked
  PRs bottom out on `staging`.
- Use a merge commit, not a squash, for a `staging` into `main` release PR. A
  squash produces phantom conflicts on the next release.
- Hotfixes branch from `main` and PR into `main`, then are immediately
  back-merged `main` to `staging`. Skipping the back-merge is how the branches
  silently diverge.
- Both accounts are `us-west-2`. `staging` carries the same rules as `main`
  because a workspace bound to that branch assumes an IAM role in a real AWS
  account: the branch is a credential, not a scratch space.

**Protection.** `main` and `staging` are covered by repository rulesets (pull
request required, force-push and deletion blocked, bypassable only by the
repository admin). Rulesets and environments are owned by the
`WebbPulse-Platform` factory, not by this repository.

## Conventions

- **Admin/management email**: use `tyler@webbpulse.com` for all management
  addresses (DMARC reporting, contact forms).
- **No em dashes** in AWS resource names, descriptions or site copy.
- **No code comments** unless explicitly asked. Docstrings are required
  everywhere and should be concise.
- Nothing is clicked in the console. Infrastructure changes go through
  Terraform.
