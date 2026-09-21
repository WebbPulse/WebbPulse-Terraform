# Run recency rollout

Old runs lack `collection`, so creating `by_recency` alone does not index them.
Actor data cannot be recovered; this migration only adds `collection = "run"`.

1. Apply the reviewed Terraform GSI addition through HCP before deploying the
   runs backend. Verify the actual plan contains the in-place index addition,
   and wait for `by_recency` to become `ACTIVE` in the target environment.
2. Deploy the runs backend that stamps `collection` on new rows. Do not enable
   global-list consumers until the migration is complete. Existing scoped reads
   continue to use `by_workspace`.
3. With approved runtime credentials, run from `backend/` using the explicit
   target table name and region:

   ```sh
   uv run --no-sync python scripts/backfill_run_collection.py --table TABLE --region us-west-2
   uv run --no-sync python scripts/backfill_run_collection.py --table TABLE --region us-west-2 --write
   ```

   These commands use no application settings or secret reads. The default is a
   dry run. Each invocation scans at most ten pages of 100 evaluated items and
   projects only `run_id`, `workspace_id`, and `collection`. Continue with
   `--after NEXT_AFTER` until `next_after` is null. If interrupted, restarting
   from the beginning is safe: conditional updates are idempotent and cannot
   recreate deleted rows or overwrite actor data. No rollback removes this
   harmless attribute.
4. Repeat a complete dry scan from the beginning and confirm zero eligible rows.
   After GSI propagation, compare paginated global and scoped run IDs for existing
   workspaces, including old runs with `actor: null`. Confirm no semaphore row is
   listed. Then enable global-list consumers. Repeat these gates for production.

Do not merge this combined infrastructure/backend PR and rely on racing deploys:
apply its reviewed infrastructure configuration first through the established HCP
workflow, or separate the infrastructure release before the backend release.
No live migration or apply is performed by the unit tests.
