# Atomic explicit prompt attachments (unactivated worktree)

Negotiate `gateway.capabilities.result.prompt_explicit_attachments_v1 === true`
on the actual current owner connection. Source files, a health commit, and this
worktree are NOT evidence that the enrolled running owner supports the feature.
Do not send unknown fields to an old owner or fall back to attach-then-submit.

`prompt.submit` retains its existing session/profile stamps and text contract:

```json
{
  "session_id": "backend-stamped runtime ID",
  "profile": "backend-stamped profile",
  "text": "inspect this",
  "attachment_mode": "explicit",
  "attachments": [{
    "kind": "image",
    "mime_type": "image/png",
    "content_base64": "strict base64 of verified raster input",
    "filename": "example.png"
  }]
}
```

- `attachment_mode` defaults to `legacy`; only `legacy` and `explicit` exist.
- Explicit omitted/empty `attachments` never consumes session `attached_images`.
  Use this also for text and server-returned generic file references.
- Filename is optional, a display label only; no filesystem path is accepted.
- PNG/JPEG/WebP only, static single-frame images; MIME and codec must agree.
- At most four images, 10 MiB each input/output, 25 MiB aggregate input/output,
  40 million pixels per image. Bound base64 length before decoding.
- All images validate and normalize before any turn admission. EXIF orientation
  is applied; metadata/trailing content is stripped by re-encoding static PNG.
- Files have private server-assigned names in the SESSION profile's existing
  `images/explicit-turns` directory (launch home for launch-profile sessions).
  A client profile stamp cannot redirect an explicit turn to a foreign profile.
- `4000` means invalid mode/payload/profile stamp; `5027` means staging I/O failure.
  Existing busy, ownership, truncation, storage and retirement errors remain.

## Admission and lifetime

Idle/deferred-build, queued drain, compute dispatch and synchronous pipe-failure
inline fallback use the same chosen image bundle, including an explicit empty
bundle. Legacy drafts are claimed under the admission lock, not after an agent
build or later queue drain. New pastes do not migrate into a previously admitted
turn. An idle-to-busy race re-enters busy policy instead of starting a second
runner. No image decoder/provider/compute dispatch runs under history_lock.

Busy explicit media is a distinct FIFO envelope and never steers, redirects,
merges into a prior media turn or interrupts the active turn. Text-only explicit
messages retain normal busy policy without touching the shared draft queue.

Refused submissions, failed/cancelled deferred builds and stopped queued bundles
release only operation-owned media. Windows retains an object handle acquired
from the creating stream before any pathname can be substituted. Cleanup uses
`ReOpenFile` on that retained object for DELETE access and
`SetFileInformationByHandle(FileDispositionInfo)`; no pathname unlink fallback
exists. The digest check is only a conservative eligibility gate, not deletion
authority. Metadata mismatches reject before any digest read; digest reads are
hard-bounded to 10 MiB plus one byte and verify opened-file metadata before and
after reading. Cleanup releases ownership in finally, including digest failures.
Replacement/redirected paths and Desktop drafts cannot be deleted.
Handle close and deletion are serialized so concurrent cleanup cannot reuse a
closed handle. Handles are closed without deleting when bundles leave scope.

During Windows staging every directory ancestor is opened no-follow, checked
with handle attributes, and held with read-only sharing (no write/delete sharing)
to exclude renames and reparse writers. POSIX staging walks no-follow directory
FDs and creates files relative to the retained final directory FD. Portable POSIX
has no atomic unlink-by-file-object: cleanup deliberately retains files instead
of risking deletion of a replacement. POSIX behavior requires its native CI lane;
it was not executed on this Windows host.

A removed queue head remains in `_dispatching_queued_prompt` until its outcome
settles. Stop/close can clean it during preparation. Explicit worker False,
and pre-dispatch failures clean the bundle. Generic compute error responses
do not prove nonacceptance: a complete turn.start may already have reached the
pipe reader before flush fails. These errors, accepted dispatches and other
unknown outcomes retain media after the dispatch-start boundary. Generation-only
re-anchors preserve the legacy restore behavior; Stop/close never resurrect an
explicit cancelled envelope. Process retirement admission stays reserved through
preparation and dispatch, without holding history_lock across provider calls.

Accepted media is retained for history. Fail-closed leftovers and accepted files
share a 256 MiB on-disk admission budget per profile explicit-turns directory:
each process serializes its own stage/check/write and counts existing entries
before writing. This is not a cross-process atomic quota or protection against
arbitrary external disk writers. Exhaustion refuses further media with 4000;
explicit empty/text submits remain usable. There is no unsafe TTL sweeper.
Reclamation is an explicit operator action after quiescing owners and deciding
which historical media to keep, not automatic pathname deletion.

This patch does not introduce an independent writer, credential handling,
transport retry policy or transcript store.

## Activation gate (separate approval required)

1. Review/apply the patch upstream and build its normal Hermes runtime/clients.
2. Obtain explicit approval for installed update and owner lifecycle operation.
   Neither installed editing nor restarting is part of this worktree task.
3. Use the existing safe owner retirement/activation workflow; preserve canonical
   stored session, profile, workspace and active work. Never restart for convenience.
4. Reconnect the companion and read back capabilities from that exact live owner.
   Fail closed unless `prompt_explicit_attachments_v1` is true.
5. Only with further user consent, run the two-client image acceptance against
   the canonical conversation, verifying genuine bytes and durable history IDs.

No installed source/config/index, live prompt, credential lookup, owner restart,
or production capability verification was performed by this implementation.
