# Branch hygiene

This is the ledoent fork of [OCA/server-tools](https://github.com/OCA/server-tools).

Most version branches (`18.0`, `19.0`, etc.) track OCA upstream verbatim — no fork-specific commits. Use those for normal Odoo deployments.

## Aggregate branches (temporary, fork-only)

Branches whose name ends in `-aggregated` or `aggregated-*` are **temporary aggregates** holding OCA upstream + one or more open OCA PRs cherry-picked or merged in. They exist solely as a deployment target while we wait for the upstream PRs to merge.

| Branch | Base | Aggregates | Tracking upstream |
|---|---|---|---|
| `aggregated-19.0` | OCA `19.0` | [OCA/server-tools#3545](https://github.com/OCA/server-tools/pull/3545) — sentry addon 19.0 port | Open since 2026-03-12 |

### Rules for aggregate branches

1. **Do not land unrelated PRs** into these branches. They aggregate upstream PRs only.
2. **Do not protect** these branches — they're force-pushable for rebase-onto-OCA.
3. **Retire on upstream merge** — once the OCA PR lands, delete the aggregate branch and update the consumer manifest (`ledoent/infra/deployments/openupgrade-lab/odoo-config.yaml` `addons-19.txt`) to point back at the upstream OCA branch.

### Updating an aggregate

```sh
git fetch origin <upstream-branch>
git checkout aggregated-<upstream-branch>
git reset --hard origin/<upstream-branch>
gh pr diff <PR-number> --repo OCA/server-tools | git apply
git add . && git commit -m "[MIG] <addon>: rebase onto upstream <branch>"
git push -f origin aggregated-<upstream-branch>
```

The consumer deployment re-fetches `--depth=1` on every pod restart, so the force-push reaches production on the next rollout.
