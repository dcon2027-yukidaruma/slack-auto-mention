# Coordinated repository rename

Approved destination: `dcon2027-yukidaruma/yukidaruma-bot` (same public repository).
The Slack application owns both roster mentions and Slack-to-Discord mirroring.
The independent Projects/weekly notification application remains a separate codebase.

The GitHub rename and the installed VM delivery agent must be migrated together.
Preparing or pushing this branch does not perform that migration. In particular,
`pull_cd.py` in a GitHub commit does not update the host's installed agent.

## Fixed invariants

- Verify repository ID `1436489838`, workflow ID `398926208`, workflow path,
  protected main, public visibility, required CI jobs and forward-only revisions
  before and after rename. Never relax repository identity or redirect checks.
- The API/codeload paths use the new canonical repository name. The expected
  archive root is derived from that name; an archive with the old root is rejected.
- Retain the current `slack-auto-mention` service/container names, Docker alias,
  ingress network, roster-reader socket, private config and SQLite paths. A GitHub
  rename does not justify creating another queue or renaming these live resources.
- Do not change Slack's Request URL, Discord application identity, credentials,
  scopes, recipients, source/destination channel grants or GitHub visibility.
- Do not reuse the old repository name for another repository; that removes
  GitHub's redirect for existing clones and links.

## Migration order

1. Obtain the existing authenticated VM session with its pinned host key. Inspect
   actual revisions, service/queue health, deploy lock, profile/config digests and
   current timer state. Stage the reviewed new agent and host mirror worker under
   immutable release directories. Retain the current installed agent and its hash.
2. Stop only `slack-auto-mention-cd.timer` and wait for any running CD service to
   finish. Keep the current bot, roster reader and Projects/weekly service running.
   Hold the existing deployment lock for the migration. A VM connection failure
   before this step means the GitHub rename must wait; branch push/PR/CI can proceed.
3. Rename the existing GitHub repository to `yukidaruma-bot`. Read it back and
   verify the same numeric identity, main revision, protection, Actions workflow,
   deployment variables and public visibility. Reconcile an unknown outcome by
   reading repository identity, without issuing another rename blindly.
4. Update the working clone's `origin` to the new canonical URL. Install the exact
   reviewed rename-aware `pull_cd.py` on the VM (not an unreviewed remote script),
   preserving file ownership/modes and all existing runtime/profile settings.
   Update the operator's recorded agent hash. The pinned profile SHA stays the
   same if its bytes have not changed. Do not use an old activation script that
   asserts the previous agent hash without reviewing its migration.
5. Require successful CI for the exact implementation commit and a reviewed main
   update. Run the new agent's read-only plan against the renamed repository.
   Confirm canonical API/archive access with no redirect or credential fallback,
   numeric identities and the exact eligible main revision. No test messages or
   historical replay is needed.
6. Provision/start the separate mirror worker and activate only the verified live
   channel mapping as described in DISCORD-MIRROR.md. Keep the current roster mode
   and ledger. Apply the existing container switch procedure; read back revision,
   health, worker heartbeat, existing mention receipts and mirror counts.
7. Release the deploy lock, resume the previously enabled CD timer, and verify a
   later normal check completes under the new canonical name. Do not claim full
   migration until both the host agent and that follow-up check are verified.

If migration fails, leave the timer paused, retain existing bot/state and report
the actual repository name, agent version and pending step. Restore a coordinated
name/agent pair only after checking both sides; never restore old SQLite receipts.

GitHub metadata read on 2026-11-11: public, main, Pages disabled, no repository
webhooks, deployment enabled with `vm-pull`. Organization/App webhooks and current
VM settings must still be inspected if they become relevant; those are not proven
absent by the repository-hook result.

Reference: [GitHub repository rename behavior](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).
