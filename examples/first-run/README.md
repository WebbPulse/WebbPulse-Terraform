# first-run

A configuration with no AWS resources, for the first end to end run on staging.
One `random_pet` and one output: enough to prove the runner unpacks the config,
assumes the run role, takes the state lock and writes state, without needing any
permission beyond the state bucket.

No backend block. The runner writes `zz_webbpulse_backend_override.tf` into the
working directory, so a config that declares its own backend would collide.
Leave the workspace's working directory empty; this config is the tarball root.

## Steps

1. Sign in at `https://staging.terraform.webbpulse.com/`.

2. Create a workspace. Engine `terraform`, engine version `1.16.3`, which is the
   version in `runner/versions.env`. Note its `ws-` id.

3. Set `example_workspace_id` to that id on the staging HCP workspace
   (`ws-xVqd4ioXLARZhGd4`), by hand, then apply. The apply creates
   `webbpulse-terraform-staging-example-run-role` and returns its ARN as the
   `example_run_role_arn` output.

4. Set that ARN as `run_role_arn` on the workspace. The field is required at
   create and editable afterwards through `PATCH /api/v1/workspaces/{id}`:

   ```bash
   curl -X PATCH "$API/api/v1/workspaces/$WS" \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"run_role_arn":"'"$ROLE_ARN"'"}'
   ```

5. Build the tarball from this directory. The upload is signed for
   `application/gzip`, so it must be gzipped, and the config has to sit at the
   root of the archive:

   ```bash
   tar -czf first-run.tar.gz -C examples/first-run .
   ```

6. Ask for a config version and upload. `size_bytes` is the ceiling signed into
   the URL, and every header the response returns is inside the signature, so
   the PUT must send each one exactly:

   ```bash
   SIZE=$(stat -c %s first-run.tar.gz)
   curl -X POST "$API/api/v1/workspaces/$WS/config-versions" \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d "{\"size_bytes\": $SIZE}"

   curl -X PUT "$UPLOAD_URL" \
     -H 'Content-Type: application/gzip' \
     -H "Content-Length: $SIZE" \
     --data-binary @first-run.tar.gz
   ```

7. Start the run from the workspace's configuration versions tab, or
   `POST /api/v1/runs` with the workspace and config version ids. The plan runs
   under the read only session policy.

8. Confirm the run once the plan reports one resource to add. The apply runs
   under the role itself, writes state to
   `workspaces/<workspace_id>/terraform.tfstate` and prints the pet name.

## Cleanup

Leave `example_workspace_id` set while the workspace exists: clearing it removes
the run role, and the next run on that workspace fails to assume it.
