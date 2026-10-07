# RFC: Goal Channel Collaboration v0

- Status: Draft
- Scope: provider-backed collaboration channels for one LoopX goal
- Decision type: product architecture and staged integration contract

## Summary

This RFC introduces **Goal Channel** as the LoopX-owned abstraction for an
external collaboration channel bound to exactly one goal. A channel may be a
Lark/Feishu group, Slack channel or thread, GitHub issue, Linear thread, or
another provider surface. The provider owns delivery and UI primitives; LoopX
owns goal state, todos, human gates, quota, evidence, receipts, and accepted
state transitions.

The first provider target is Lark/Feishu:

- create or reuse one group chat for a goal;
- create or reuse a Lark Base Kanban projection;
- pin a compact control message and Kanban link in the group;
- send bounded human-gate notifications;
- sync accepted LoopX state back to the Kanban projection.

The channel is not the source of truth. It is the visible collaboration entry
point and feedback surface for a LoopX goal.

## Problem

LoopX already has durable state and Lark-specific pieces:

- `lark-kanban` projects LoopX todos and status into Lark Base;
- Lark notification code paths already prove send, readback, idempotency, and
  profile checks for narrower domains.

These pieces do not yet compose into the product shape users expect from a
Claude Tag-like workflow:

1. Mention a bot in a collaboration surface.
2. Get or create an isolated collaboration channel for that objective.
3. See progress and gates in the same place.
4. Receive human-gate prompts without leaving the collaboration surface.
5. Let LoopX keep the authoritative goal, todo, gate, and evidence state.

Without a first-class Goal Channel abstraction, a Lark group, Base board,
message thread, pinned status, and notification receipts can drift apart.

## Goals

- Define a provider-neutral LoopX concept for "the external collaboration
  channel for this goal".
- Keep Lark group chat, Kanban, pinned messages, and gate notifications bound
  to one `goal_id`.
- Preserve LoopX as the only writer of canonical goal, todo, gate, evidence,
  and quota state.
- Make provider writes explicit, previewable, idempotent, and readback
  verified.
- Let humans see and answer gate prompts in the channel without granting the
  channel broad write authority.
- Allow later provider adapters such as Slack, GitHub, or Linear without
  renaming the core concept.

## Non-Goals

- Replacing `lark-kanban`; Goal Channel composes it.
- Making Lark, Slack, or any external tool the source of truth.
- Shipping a LoopX-managed global Lark app in the open-source CLI.
- Requiring one fixed bot identity across all users or tenants.
- Treating arbitrary chat text as an accepted state transition.
- Copying raw chat history, private message ids, local paths, credentials, or
  raw provider payloads into public packets.
- Solving full remote runner orchestration in this RFC.

## Naming

Use **Goal Channel** for the core abstraction.

Avoid `room` as the primary name. A group chat may be one implementation
detail, but a Goal Channel can contain chat, pinned status, Kanban,
notification receipts, and provider-specific metadata.

Suggested command surface:

```bash
loopx goal-channel setup --provider lark --goal-id <goal-id>
loopx goal-channel target add --name <target> --provider lark ...
loopx goal-channel setup --goal-id <goal-id> --target <target>
loopx goal-channel attach --target <target> --goal-id <goal-a> --goal-id <goal-b>
loopx goal-channel configure --goal-id <goal-id> --auto-notify-human-gates
loopx goal-channel doctor --goal-id <goal-id>
loopx goal-channel sync --goal-id <goal-id>
loopx goal-channel notify-gate --goal-id <goal-id>
loopx goal-channel runtime setup --goal-id <goal-id> --bot-id <bot-id> --chat-id <chat-id>
```

`goal-channel` is the durable control-plane object and the user-facing CLI.
The optional [botmux runtime integration](../../integrations/botmux-goal-channel-runtime.md)
delegates IM delivery and persistent agent sessions to botmux without changing
Goal Channel or LoopX state authority. Delivery does not imply a state
transition; the configured agent runtime must still invoke LoopX explicitly.

## Ownership Model

| Capability | LoopX | Provider channel | Provider adapter |
| --- | --- | --- | --- |
| Goal lifecycle | Owner | Projection | Calls LoopX |
| Todos, claims, gates, quota | Owner | Projection and prompts | Syncs bounded packets |
| Kanban rows | Source data owner | Display owner | Upserts rows |
| Group/chat/thread | References binding | Owner | Creates, updates, reads |
| Pinned status | Builds bounded content | Displays | Sends and pins |
| Human gate question | Owner of question and cooldown | Delivery | Sends and verifies |
| Credentials and profile | Never stores secrets | Provider auth | Uses local-private profile |
| Receipts | Owner of accepted transition receipts | Message ids are private | Records compact send/readback receipt |

The provider may store its own state. LoopX stores only the minimum local-private
binding needed to operate the channel.

## Lark Provider Binding

A Lark Goal Channel binding is local-private and project-scoped:

```json
{
  "schema_version": "loopx_goal_channel_lark_binding_v0",
  "goal_id": "loopx-goal",
  "provider": "lark",
  "enabled": true,
  "channel": {
    "chat_id": "oc_<private-chat-id>",
    "chat_name": "LoopX - loopx-goal",
    "pinned_message_id": "om_<private-message-id>"
  },
  "kanban": {
    "base_token": "<private-base-token>",
    "table_id": "tbl...",
    "view_ids": {
      "Kanban": "vew...",
      "User Gates": "vew..."
    }
  },
  "identity": {
    "mode": "project_bot",
    "sender_profile": "loopx-project-bot",
    "sender_identity": "bot",
    "bot_display_name": "LoopX Bot"
  },
  "receipts": {}
}
```

The file belongs under `.loopx/` or another ignored local-private path. Public
status packets must not expose chat ids, member ids, message ids, profile names,
raw Lark payloads, local file paths, or credentials. Public packets may expose
booleans, counts, sanitized provider labels, and operator-safe URLs only when
the caller has already chosen to show them.

### Shared provider targets

Several Goal Channels may reference one named local-private provider target.
The target owns the reusable Lark chat and sender identity; each Goal binding
continues to own its control message, Kanban, receipts, and cooldown state:

```bash
loopx goal-channel target add \
  --name loopx-dev \
  --provider lark \
  --chat-id <private-chat-id> \
  --bot-app-id <private-app-id> \
  --execute

loopx goal-channel attach \
  --target loopx-dev \
  --goal-id goal-a \
  --goal-id goal-b \
  --execute
```

The target store belongs under the resolved LoopX runtime root and is never a
public or repository-tracked configuration. A target-linked Goal binding stores
only `target_ref` plus Goal-local state; changing a target updates the resolved
chat or sender for every referencing Goal without merging their state.
After moving a target to another group, rerun bounded `attach` batches so each
Goal can establish and verify its own control message in the new group.

Machines do not synchronize private chat ids or authentication profiles. To
use the same group from a workstation and a development host, configure the
same target name independently on each machine. Inbound routing must reply to a
specific gate message or carry an explicit Goal id; ordinary group text must
never be inferred as belonging to one of several Goals.

## BYO Provider Identity

Open-source LoopX should default to **Bring Your Own provider identity**:

- users create or select their own Lark app or bot in their tenant;
- users authenticate it through `lark-cli` or a future provider-specific
  profile manager;
- LoopX stores only the local profile reference and compact verification state;
- LoopX never ships a fixed cross-tenant bot as an implicit dependency.

Supported identity modes:

| Mode | Intended use | Tradeoff |
| --- | --- | --- |
| `local_user` | Create and own the group and Base as the user | Easy resource ownership; the bot is still required for messages |
| `project_bot` | Use a dedicated bot profile for channel messages | Requires app/bot setup but gives stable message identity |
| `managed_app` | Future hosted product | Best UX, requires tenant install, compliance, and operations |

The first implementation uses the local user identity for group and Base
operations. Goal Control messages, pins, and gate notifications always use the
configured bot identity. It does not require or request
`im:message.send_as_user`.

Effectful direct setup requires an explicit `--bot-app-id cli_...`; target-based
setup obtains that explicit selection from the local-private target. LoopX
verifies that it matches the selected `lark-cli` profile before adding the bot or
sending a message. Omitting the flag is a preview-only convenience, not
authorization to select the default profile's bot.

## Lifecycle

### Setup

`goal-channel setup --provider lark --goal-id <goal-id>` should:

1. Resolve and validate the goal.
2. Load or create the local-private Lark channel binding.
3. Verify `loopx-lark` extension activation and required permissions.
4. Verify the local user resource identity and the configured bot sender.
5. Create or reuse a Lark group chat and verify the bot is a member.
6. Create or reuse the Lark Kanban Base through `lark-kanban`.
7. Read back and persist the canonical Base URL.
8. Send a compact Goal Control message containing the Kanban link.
9. Pin that verified control message.
10. Save the local-private binding and compact receipts.

Default mode is dry-run. External writes require `--execute`.

### Sync

`goal-channel sync` composes existing projections:

- `lark-kanban sync-loopx-todos` for active user/agent todos and derived
  domain outcomes;
- a compact status/control message update or append when the visible channel
  summary changed materially;
- optional periodic report or explore projection sinks only when separately
  configured.

The sync command must not create new canonical todos from remote rows.

### Human Gate Notification

`goal-channel notify-gate` sends a bounded message when LoopX already decided a
human gate or user todo needs attention. The trigger input is the existing quota
and interaction-contract surface:

- `state=operator_gate`;
- `notify_user_on_gate=true`;
- `notify_user_on_open_todo=true`;
- `gate_prompt`;
- `operator_question`;
- `open_todo_notify_reason`;
- `user_todo_summary`;
- `user_gate_notification_cooldown`.

The message includes:

- goal label and short objective;
- concrete gate question;
- up to three user-gate or user-action todos;
- expected reply format;
- Kanban link or channel control link;
- next safe action while waiting, if any.

It excludes local paths, raw active state, private logs, credentials, message
ids, and raw provider payloads.

Automatic delivery is disabled by default. After Goal Channel setup, preview
and then enable it explicitly:

```bash
loopx goal-channel configure --goal-id <goal-id> --auto-notify-human-gates
loopx goal-channel configure --goal-id <goal-id> --auto-notify-human-gates --execute
```

Once enabled, each successful non-dry-run `refresh-state` rebuilds quota from
canonical LoopX state. It sends only when quota selects a human gate, and it
reuses the same bot verification, semantic idempotency, cooldown, provider
idempotency key, and message readback as `notify-gate`.

Use `loopx refresh-state ... --suppress-external-sinks` to suppress delivery for
one refresh without disabling the binding. Turn-bound recovery remembers that
pause and requires `--resume-external-sinks <resume_key>` to resume delivery using
the returned key. This acknowledges the current operation, not new permissions;
historical operations without pause evidence retain their previous behavior.
See the [recovery handshake](../../state-interaction-model.md). Disable automatic
delivery persistently with:

```bash
loopx goal-channel configure --goal-id <goal-id> --no-auto-notify-human-gates --execute
```

The opt-in is stored only in the project-local private Goal Channel binding.
It does not grant repository or LoopX transition authority. Chat replies can
provide context, but a gate changes only after LoopX validates and records the
corresponding decision.

Automatic lifecycle delivery resolves the enabled, doctor-verified Lark
extension before reading the private binding. Enabling automatic delivery
requires the canonical project-local binding path; custom `--binding-path`
values are rejected because `refresh-state` has no per-invocation path input.
The explicit local disable command is the only recovery exception: it may clear
the opt-in while the extension or binding is incomplete, and it never enters
provider code or performs an external write.

Enabling also writes an owner-only local marker containing only the enabled
boolean. The lifecycle may read this marker before extension activation solely
to distinguish a never-configured project from a configured sink whose
extension became unavailable. The latter fails closed with a retryable
`extension_unavailable` postcondition; the marker contains no provider ids,
credentials, channel metadata, or raw payloads.

## Command Contract

Each effectful command returns a compact packet:

```json
{
  "schema_version": "loopx_goal_channel_operation_v0",
  "ok": true,
  "goal_id": "loopx-goal",
  "provider": "lark",
  "operation": "notify_gate",
  "execute": true,
  "external_write_performed": true,
  "readback_verified": true,
  "idempotency_key": "sha256:...",
  "receipt_id": "receipt_...",
  "public_summary": "sent one gate notification to the configured Lark channel",
  "private_provider_payload_captured": false
}
```

Failures should be typed:

- `extension_unavailable`;
- `provider_identity_unverified`;
- `channel_binding_missing`;
- `channel_membership_unverified`;
- `kanban_binding_missing`;
- `notification_cooldown_active`;
- `readback_mismatch`;
- `state_transition_rejected`;
- `provider_api_failed`.

## Idempotency And Cooldown

Provider writes use idempotency keys derived from the semantic action, not the
wall-clock attempt:

```text
goal_id + provider + operation + todo_id/gate_id + gate_text_hash + channel_id
```

Rules:

- retrying the same send returns `already_sent` or the original receipt;
- gate text changes may create a new notification key;
- cooldown suppresses repeated reminders without closing the gate;
- stale provider events cannot override newer LoopX revisions.

## Security And Privacy

- Channel membership is not LoopX write authority.
- Bot membership is verified before sending.
- Message readback is required before recording a successful send receipt.
- Raw provider payloads stay local-private.
- Shared/global registry calls resolve Goal Channel state beside the selected
  goal's canonical `source_registry`; caller CWD is never a default state root.
- Local-private JSON uses an owner-only temporary file plus atomic replace, so
  interrupted writes do not expose or truncate the previous binding.
- Local checkout paths, active-state paths, credentials, chat ids, member ids,
  message ids, and profile names do not enter public artifacts.
- The channel may show a Kanban link, but the Kanban remains a projection.
- Destructive, credentialed, production, publish, merge, or external-write
  gates remain LoopX gates and cannot be bypassed by chat text.

## Smallest Useful Slice

The smallest useful implementation should be:

1. Add `loopx goal-channel` with `setup`, `configure`, `doctor`, `sync`, and
   `notify-gate`.
2. Implement only the Lark provider.
3. Reuse existing `lark-kanban` setup/sync and `loopx-lark` extension
   activation checks.
4. Create or reuse one Lark group for one existing goal.
5. Send and pin one compact Goal Control message.
6. Send a human-gate notification with idempotency and readback.
7. Optionally send LoopX-selected human gates after authorized `refresh-state`
   writes.
8. Store local-private binding, automation opt-in, and receipts under `.loopx/`.

This slice proves the external collaboration entry point.

## Validation

The first slice must prove:

- setup is dry-run by default and performs external writes only with
  `--execute`;
- one goal maps to one local-private Lark binding;
- extension activation is checked before private config is read;
- a Kanban board can be reused or created and then synced;
- a Goal Control message is sent, pinned, and readback verified;
- human-gate notification respects cooldown and idempotency;
- repeated notification retries do not duplicate visible messages;
- automatic delivery is disabled by default and can be suppressed per refresh;
- automatic delivery reads canonical quota and does not send for non-gate state;
- doctor reports missing bot auth, missing channel, missing Kanban, or stale
  extension activation with typed blockers;
- local-private binding files remain ignored and untracked;
- public packets do not contain chat ids, member ids, message ids, profile
  names, local paths, raw provider payloads, or credentials.

## Agent-scoped conversation proposal

The following shared session and ingress design remains proposed. Moving it here
does not enable an adapter, replace shipped Goal Channel behavior, or qualify
Web/Lark convergence. Session identity and mode admission belong to
[Agent Session Execution Modes](agent-session-execution-modes-v0.md).

## Agent-scoped Web and Lark convergence

The short-term collaboration product is not a separate status Bot. It is a
second frontend transport for an Agent's real working session:

```text
LoopX Goal
  -> Agent A
       -> working session A (attached or managed)
            -> Web Chat
            -> Lark Bot connection A
  -> Agent B
       -> working session B (attached or managed)
            -> Web Chat
            -> Lark Bot connection B
```

In v0, each Agent may have at most one active `lark_bot` connection. This is a
logical Agent-to-connection binding; it does not require a unique Lark
application credential for every Agent. One Bot application may serve multiple
connections if the local broker preserves explicit Agent and channel routing.

### One ordered working conversation

The baseline project coordinator surface is the existing **Goal → Chat**. A
registered peer may carry that responsibility instead; neither choice creates
a separate coordinator conversation or changes the steward's cross-Goal role.
[Explicit Codex continuation](../../reference/goal-chat-continuation.md) reuses
the composer with explicit enable/pause/continue, streamed work and original local
history. It joins the existing delegation service and independently accepted
member results; queue/inbox/steer retain distinct receipts. First-use settings
select an existing execution binding without granting authority by registration.
Lark/other-lead parity and unattended operation remain separate requirements.

In live-steering and queued-session modes, Web and Lark messages enter one
serialized ingress stream for the selected Agent session. Each message records
public-safe transport metadata such as `origin=web` or `origin=lark`, but origin
does not select a different Agent, conversation history, executor, or LoopX
state machine.

The session router assigns ordering before delivery to the runtime. A
simultaneous Web and Lark message may wait, interrupt through an explicit
control action, or fail closed according to session policy; it may not create
two concurrent Agent attempts. Responses may be projected to both surfaces
according to connection policy while preserving one canonical sequence.

An asynchronous inbox event is different: it remains owner-private external
input until the selected Agent drains and interprets it. Only the accepted
Agent-facing message or resulting durable effect joins the working-session
sequence. Provider collection alone does not create conversation history,
task authority, a Turn, or quota spend.

### Agent binding, not Goal-wide or runtime-specific binding

A Lark connection binds to a concrete Agent within a Goal. If a Goal has
multiple Agents and the connection does not identify one, routing fails closed.
The Bot talks directly to that working Agent; it does not first ask a manager
Agent to classify or relay the message. The binding is not hard-coded to Codex:
the Agent's execution session may be an attached Codex App today or a managed
Pi/`dsh` session later.

## Agent-scoped external Connector model

Lark group ingress and Lark document comments are two instances of one
provider-neutral Connector boundary. A Connector binds an external source to
one registered Agent and advertises only the operations it can actually
perform:

```text
agent_external_connector_v0 = {
  goal_ref,
  agent_ref,
  provider_kind,
  source_kind,
  source_ref,             // opaque owner-local reference
  capture_policy,
  ingress_policy,
  response_policy,
  cursor_ref,
  lifecycle,
  capabilities[]
}
```

The same provider may expose several source kinds. For example, a Lark group
source may advertise live delivery, history catch-up, thread reply, and ACK,
while a document-comment source may advertise incremental listing, anchor and
reply-chain readback, comment reply, and resolved-state observation. Missing
capabilities remain unavailable; LoopX does not emulate them by scraping an
unrelated surface.

A Connector capability may also expose typed `permission_requirements` with
the provider identity, exact scopes, publication requirement, and an official
repair URL bound to the selected App. The provider extension owns those facts;
the LoopX core only renders the typed guidance. Realtime receive, response
write, and history catch-up remain separate capabilities and must not be
collapsed into one generic "message permission" flag.

### Authority material versus collaboration events

A durable document and its comments have different authority semantics:

- the document body is registered as a Goal authority material with freshness,
  revision, owner status, and conflict policy;
- a comment is owner-private external input addressed to an Agent, not an
  accepted requirement, Todo mutation, or repository fact by itself; and
- incorporating a comment requires an explicit durable effect such as a Todo
  update, accepted design revision, no-follow-up rationale, or owner gate.

Reading the body does not advance the comment cursor. Listing comments does not
make the document authoritative. A comment that contradicts accepted state is
recorded as a pending decision or evidence gap rather than silently changing
Goal truth.

### Capture, replay, and acknowledgement

Every event-source Connector owns a stable provider event id, incremental
cursor or equivalent checkpoint, bounded catch-up policy, and idempotency key.
Real-time subscription and history catch-up feed the same deduplicated inbox so
that events created before attachment or during downtime are not silently
lost. A source may be filtered by mention, author, document, comment state,
anchor, or configured source scope without changing its delivery mode.

The Agent processes one accepted event with this ordering:

```text
capture and deduplicate
  -> mark processing
  -> read fresh Goal and authority state
  -> record durable effect or explicit no-follow-up rationale
  -> send an optional response through a declared Connector capability
  -> verify provider readback
  -> ACK and advance the source cursor
```

No ACK or cursor advance may precede the durable effect and required verified
response. A crash replays the same event idempotently. Private bodies, authors,
provider ids, source references, and comment text remain in owner-local inbox
storage; status and quota see content-free urgency only.

### Delivery into the working Agent

Connector capture and Agent delivery remain orthogonal. A live group message
may steer the current working session, wait in its ordered queue, or wake an
asynchronous Agent inbox. A document comment normally enters through
`async_inbox`, but the same event may be submitted into a verified live session
when an explicit policy permits it. In all cases it targets the existing bound
Agent and never starts a shadow manager or a fresh conversation implicitly.

### Short-term Goal Channel bridge

The existing Goal Channel transport may provide the first Lark delivery path,
provided that its Goal-level connection is refined with an explicit target
Agent and is routed into that Agent's existing ordered session. This bridge is
an incremental implementation path, not permission to keep a second IM-only
conversation lifecycle.

If accepted, this Agent-scoped proposal refines the Goal-level binding constraint
above for interactive chat. Goal-wide Kanban, lifecycle
notifications, and shared collaboration artifacts may remain Goal-scoped;
inbound working conversation is Agent-scoped.

## Agent-scoped Bot ingress modes

Agent-to-Bot connections and peer collaboration need the same three explicit
ingress semantics. User-facing names are **inbox**, **queue** and **steer**;
the existing vocabulary below remains. They express delivery intent for one
bound Agent, not three Agents or a natural-language classifier. This proposal
extends the common policy to peer ingress; it does not ship a new API or change
existing adapters merely by renaming their input:

```text
agent_bot_ingress_mode_v0 =
  live_steering
  | session_queue
  | async_inbox
```

The three policies solve different availability conditions:

| Mode | Delivery target | Availability model | Durable boundary |
|---|---|---|---|
| `live_steering` | The specified active execution in the bound working session | Host can apply input at a declared safe point | Existing session/event store and consumption receipt; no second executor |
| `session_queue` | A subsequent work input in the same bound session | Current work finishes or explicitly yields its execution before delivery | Owner-local durable ordered ingress queue keyed by Agent and session |
| `async_inbox` | The next eligible LoopX Agent turn after an explicit drain | No Agent process needs to remain alive | Existing provider-owned event inbox plus content-free quota urgency |

This refines the earlier queue phrase "when it next accepts input": a host that
merges pending input into the active work has not thereby implemented the
proposed queue semantics. Qualify the change explicitly, preserving old profile
behavior until its opt-in implementation and compatibility tests pass.

Persist the requested mode, permitted fallback and actual delivery disposition
with existing ingress identity and recipient scope. Readback distinguishes
durable receipt, queued dispatch, host consumption and steering application;
work adoption/acceptance remains with collaboration/work owners. Model prose or
HTTP success is not a consumption receipt. Unknown capabilities fail explicitly.
Frontend, CLI and Lark show the effective mode, waiting reason and result on the
original work/conversation surface, rather than creating a separate team board.

### Delivery intent does not choose the wake policy

The mode determines where input may be consumed; the binding's existing
continuation owner determines whether another execution opportunity is admitted.
This proposed matrix qualifies adapters without adding a fourth ingress mode:

| Recipient condition | Required behavior |
| --- | --- |
| Active turn or pending tool | Inbox remains for explicit drain; queue waits for a subsequent turn; steer targets the exact active generation and declared safe input boundary. Acceptance cannot imply that an already submitted model/tool request was preempted. |
| Idle or turn complete | Persist eligible inbox/queue input. Only the configured continuation owner may admit a new turn after scope/budget checks; without that policy, show pending input. Steer is unavailable without an active target. |
| Finalizing or interrupted | Preserve late input/result identity without reopening the finishing turn. Recheck after finalization; explicit interruption cannot be undone by a notification. Resume follows the existing owner and pause policy. |
| Unloaded or disconnected | Persistence does not prove a live session. Recover only through the qualified binding path, revalidate scope and generation, and retain an actionable pending/unavailable observation when recovery is unsupported. |

A queued input is not a promise to start a turn; a provider's trigger flag is
not LoopX admission. Multiple accepted messages may enter one eligible turn,
but independent work requests retain their identities and return obligations.
Use the [handoff contract](capable-manager-semantic-handoff-v0.md#request-identity-and-result-routing-across-a-team)
for those relations, rather than treating one transport receipt as a join.

Project three separate facts: the ingress receipt, the actual execution/wakeup
observation, and the work result/acceptance. Never show a saved message as
"the Agent is working" or a wake notification as "result received". Notification
loss must leave the saved input/result discoverable through readback; replay or
reconnect must deduplicate application by ingress/result identity and cannot start a second executor. Extend the existing
corrected-input fixture with idle input without a wake policy, finalization races,
coalesced notifications and restart between result commit and notification.
These are design requirements; every host still needs its own qualification.

### Capture, ingress, and reply are orthogonal

Provider selection and Agent delivery must not reuse one overloaded flag. The
initial Lark group shape is:

```text
capture_scope: mentions | configured_chat_all
ingress_mode: live_steering | session_queue | async_inbox
reply_mode: source_thread | topic_reply | configured_mirror
```

`capture_scope` answers which provider events are eligible. `ingress_mode`
answers how one eligible event reaches the Agent. `reply_mode` answers where a
verified response is delivered. The existing `incoming_mode=mentions|all`
expresses capture scope only; it is not proof of session attachment.

The persisted inbox scope must equal the effective provider routing scope. An
`addressed_only` stream is never projected as `thread_complete`, even when it
has an enabled source-message reply binding. `configured_chat_all` remains an
explicit owner choice: it stores the configured conversation for domain
interpretation, but only typed questions, mentions, or verified bot replies
activate `reply_due`.

Mention admission binds both the App id and the Bot open id returned by the
verified provider profile. A rendered display name is a compatibility signal,
not the only identity proof. Every rejected provider event retains one
content-free decision reason such as `not_addressed` or `topic_mismatch` in
listener health, so an event that was seen but not persisted cannot disappear
behind a bare `ignored` status.

Fallback is explicit and defaults to fail closed. A `live_steering`
connection may opt into `session_queue` or `async_inbox` when the session is
unavailable, but it may not silently start another runtime or write the same
event to multiple modes. The selected mode, fallback decision, and dedupe key
produce one content-free ingress receipt.

### Live steering

`live_steering` submits into a verified Agent working-session binding. It
shares the Web ingress serializer, upstream resume identity, interrupt policy,
workspace, runtime, trust, and capability boundary. If that binding is stale,
ambiguous, terminal, or owned by another Agent, delivery fails closed.

Steering is transport, not task authority. Read-only input can be consumed by
the active session without claiming new work. A material effect still requires
the fresh LoopX decision, validation, writeback, and settlement appropriate to the attached or
managed execution mode.

Steer targets the current execution generation and its next supported safe input
point; it is not interrupt/restart. While an external tool is outstanding, the
host may durably accept a pending correction without claiming it was applied.
If safe injection is unavailable, report that fact and use only the request's
explicit fallback. Never fabricate a tool result to deliver the correction.
An invalidated tool call needs an explicit cancellation disposition; reconcile
its late result against the current input version and execution fence. The
message itself neither cancels all peers nor revokes their authority.

### Session queue

`session_queue` is a broker-owned buffer for a known Agent working session. It
preserves stable event dedupe, per-session order, bounded size, expiry,
backpressure, cancellation, and crash-safe dispatch. It is not the LoopX Todo
queue and may not mutate Goal priority, claim work, or grant capabilities.

After the current work ends or explicitly yields execution, the broker submits
the oldest eligible entry through normal serialized ingress. A pending-tool
idle observation alone is not that boundary. A missing or replaced session
requires an explicit rebind or dead-letter decision; it does not silently
route the entry to a fresh Agent history.

### Asynchronous inbox

`async_inbox` reuses the existing Lark event inbox and collector rather than
keeping an Agent process alive. The collector writes owner-private bounded
events. LoopX projects only `operator_inbox_urgency_v0`: pending/question/
mention/reply counts, oldest age, and `reply_due`, never message bodies,
senders, provider ids, private paths, or chat ids.

When `reply_due=true`, the inbox lane preempts ordinary advancement and monitor
work at the next eligible admission; this does not interrupt an active execution.
The selected Agent drains bounded content, interprets it against fresh
Goal state, writes any durable effect first, sends at most one idempotent
source-thread reply with provider readback, and only then ACKs. Drain alone is
read-only; collection or ACK is never semantic authority.

The Goal Topic compatibility runtime currently composes provider collection,
an Inbox file, a Goal Chat answer, reply, and ACK inline. That path is useful
evidence but is not Agent-scoped convergence when it opens a generic Agent
session or fails to register inbox urgency on the bound Goal. The implementation
must split provider collection from ingress policy, require the registered
Agent id, and either submit through a verified working-session binding or
publish the inbox pointer to the canonical quota path.

Qualify the three modes with one corrected-input fixture: pending tool, busy and
offline recipient, expired message, full queue, duplicate/conflicting identity,
session replacement, sender revocation and late tool result. Assert the actual
consumption boundary and fallback, not just message existence. Inbox drain must
not claim work acceptance; queue must not alter active work; steer must not claim
application before the host receipt. These are proposed acceptance requirements,
not evidence that every host currently supports all modes.
