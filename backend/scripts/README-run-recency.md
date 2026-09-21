# Run recency rollout

Old runs lack `collection`, so creating `by_recency` alone does not index them.
Actor data cannot be recovered; this migration only adds `collection = "run"`.

1. Main finishes the PR70 apply, then reviews and applies the infrastructure-only
   [PR71](https://github.com/WebbPulse/WebbPulse-Terraform/pull/71) through HCP.
   Verify the actual plan contains the in-place index addition, and explicitly
   verify `by_recency` is `ACTIVE` on the target environment's runs table before
   merging PR68. A green HCP check or table status alone is insufficient.
2. Merge PR68 and verify the runs backend image digest and successful deployment
   match that release. It stamps `collection` on new rows. PR68's frontend changes
   are compatibility only: generated contracts and cursor handling for existing
   workspace-scoped requests. They work with both the old unpaginated backend
   and the new paginated backend, so concurrent compatibility deployment is safe.
   No UI uses the global-list endpoint at this stage.
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

5. Only after main records the backend digest, complete zero-eligible dry scan,
   and paginated global/scoped ID parity, merge the stacked frontend follow-up.
   Retarget it to staging after PR68 merges. It changes the Runs page to the
   global-list endpoint. Existing deploy workflows remain unchanged.

Keep these three release boundaries separate in production too. Promote the
infrastructure first, apply and verify ACTIVE, then promote backend compatibility,
verify its deployed digest and migrate, then promote the frontend switch. A single
promotion containing all three slices would bypass the sequencing. Deployment
workflows are still independent; main enforces these gates at each merge boundary.
No live migration or apply is performed by the unit tests or this preparation.
