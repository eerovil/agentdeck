# AgentDeck

AgentDeck helps an operator observe and steer coding-agent work across providers while preserving
long-running work independently from the dashboard that presents it.

## Sessions and identity

**Provider**:
An integration that translates one coding-agent ecosystem into AgentDeck's common language.
_Avoid_: Backend, vendor integration

**Provider Account**:
One configured provider context through which AgentDeck discovers sessions and applies policy. Its
identity includes both the provider and the local account label.
_Avoid_: Account label, profile, config directory

**Session**:
A monitorable unit of coding-agent work within one Provider Account. A Session may be observable
even when AgentDeck cannot control it.
_Avoid_: Process, chat, rollout, thread

**Session Identity**:
The AgentDeck-wide identity of a Session, scoped by its Provider Account. A provider source
identifier alone is not globally unique.
_Avoid_: UUID, source ID, session ID when the scoped identity is meant

**Presentation-Eligible Session**:
A Session that passes the provider and availability policy for consideration in the dashboard. It
may still be omitted from the displayed hierarchy by relationship rules.
_Avoid_: Visible Session, displayed Session

**Displayed Session**:
A Presentation-Eligible Session that currently has a card or child row in the dashboard.
_Avoid_: Visible Session

**Operator Session**:
A Session for which the human operator is responsible and which Deckhand may consider for
attention.
_Avoid_: Top-level Session, non-delegated Session

**Background Session**:
A Session treated as supporting other work rather than requiring its own Deckhand attention. Being
background work does not by itself prove a recorded delegation relationship.
_Avoid_: Delegated Session when lineage is not known

## Session relationships

**Parent Session**:
The Session whose work contains or initiated a Child Session.
_Avoid_: Owner, root Session

**Child Session**:
A Session presented in the context of a Parent Session.
_Avoid_: Nested card, child rollout

**Embedded Subagent**:
Subagent work represented as progress within a Parent Session rather than as an independent
Session.
_Avoid_: Child Session, Delegated Session

**Native Subagent Session**:
A provider-created Child Session that is intrinsically part of its Parent Session's work.
_Avoid_: Embedded Subagent, Delegated Session, internal helper

**Delegated Session**:
Autonomous work started through AgentDeck's delegation interface and linked to the Session that
initiated it. A Delegated Session may cross Provider boundaries.
_Avoid_: Native Subagent Session, Background Session

**Delegation Lineage**:
The parent-child relationship established when one Session delegates work. Lineage recorded when
delegation starts is authoritative over relationships inferred later from provider evidence.
_Avoid_: Ownership, nesting

## Availability and activity

**Live Session**:
A Session whose local process or control source currently exists. A Live Session may be Resting.
_Avoid_: Production Session, Working Session

**Historical Session**:
A Session with a local transcript but no live local process.
_Avoid_: Idle Session

**Remote Session**:
A provider-accessible Session with no local transcript.
_Avoid_: Live Session, cloud worker

**Active Turn**:
A user turn that the Session itself is currently executing.
_Avoid_: Live Session, open Session

**Resting Session**:
A Live Session with no Active Turn.
_Avoid_: Idle Session

**Waiting Session**:
A Session paused for an operator answer or approval. Waiting is not working.
_Avoid_: Resting Session, blocked issue

**Stalled Turn**:
An apparently open turn that has produced no progress beyond the accepted threshold and is no
longer presented as working.
_Avoid_: Resting Session, failed Session

**Direct Activity**:
Work performed by a Session's own Active Turn.
_Avoid_: Effective Activity

**Effective Activity**:
The activity attributed to a Session after including current work by its descendants or Embedded
Subagents.
_Avoid_: Parent thinking, inherited Session state

**Working Session**:
A Session with Direct Activity or Effective Activity.
_Avoid_: Live Session, thinking Session

**Working Count**:
The number of top-level Sessions currently doing effective work, with descendant work counted once
through its top-level parent.
_Avoid_: Active process count, worker count

## Control and ownership

**AgentDeck Ownership**:
Authority for AgentDeck to direct a Session's delivery and Active Turn lifecycle. Ownership is not
inferred from transcript visibility, presentation classification, or a transient source label.
_Avoid_: Visibility, scheduling ownership

**Runtime Availability**:
Whether the Web Process can currently reach the Persistent Runtime for a Provider Account.
Availability determines whether control can be offered now but does not erase AgentDeck Ownership.
_Avoid_: Ownership, process liveness, health

**Capability**:
A Session action that AgentDeck may safely offer at the current moment. Capabilities are dynamic
facts derived from the Session and its control environment, not permanent permissions.
_Avoid_: Entitlement, feature flag, ownership

**Message Injection**:
Submission of a follow-up message to an existing Session that is not currently executing an Active
Turn.
_Avoid_: Active-Turn Steering, delivery

**Active-Turn Steering**:
Submission of an instruction to an already-running Active Turn in a Session under AgentDeck
Ownership.
_Avoid_: Message Injection, queued follow-up

**Pending Interaction**:
A provider-neutral request for an operator answer or decision that pauses an Active Turn.
_Avoid_: Question when approvals and decisions are also possible

**Persistent Runtime**:
The long-lived AgentDeck control component that owns agent execution across Web Process
deployments.
_Avoid_: Codex service, backend, Web Process

**Web Process**:
The restartable AgentDeck component that presents state and requests control without owning
long-lived agent execution.
_Avoid_: Persistent Runtime, server

**Deck-Owned Claude Worker**:
An AgentDeck-controlled, long-lived Claude execution associated with durable worker lineage.
_Avoid_: Worker when the provider matters, Claude Session

**Deck-Owned Codex Thread**:
An AgentDeck-created or recovered Codex conversation under Persistent Runtime control.
_Avoid_: Worker, app-server source

## Deckhand and titles

**Deckhand**:
The optional operator-support capability that provides Attention Triage and Semantic Titles.
_Avoid_: Assistant, agent assistant

**Attention Triage**:
The assessment of whether an Operator Session currently requires operator attention.
_Avoid_: Monitoring, classification

**Attention Insight**:
A current, actionable Deckhand finding for one Operator Session.
_Avoid_: Alert, notification, card

**Triage Verdict**:
Deckhand's conclusion that a resting Session is blocked or finished. Either verdict can still
require operator review.
_Avoid_: Acknowledgement, merged, shipped

**Acknowledgement**:
The operator's temporary acceptance of the current Attention Insight. It is not a claim that the
underlying work is complete.
_Avoid_: Completion, resolution, shipped

**Deckhand Status**:
The effective operator-facing attention state of a Session, distinct from its availability and
activity.
_Avoid_: Session status, liveness, activity

**Native Title**:
The provider-supplied title of a Session.
_Avoid_: Original title, raw title

**Semantic Title**:
A Deckhand-generated description of a Session's current objective.
_Avoid_: Generated title, AI title

**Display Title**:
The title shown by AgentDeck: the Semantic Title when present, otherwise the Native Title, with
stable issue identity retained where applicable.
_Avoid_: Session title when the source matters

## Delivery environments

**Staging Environment**:
The parallel AgentDeck stack used to validate changes before production. Its application state is
separate, but its agent accounts, filesystem effects, and provider budget are real.
_Avoid_: Sandbox, isolated execution environment

**Canary Validation**:
Validation of a change in the Staging Environment before Promotion.
_Avoid_: Staging deployment

**Production Environment**:
The AgentDeck stack serving the production dashboard and runtime.
_Avoid_: Live Session, live environment

**Promotion**:
The controlled transfer of validated staging commits into the production branch.
_Avoid_: Deployment, release

**Deployment**:
Application of a production revision to the relevant production service or services.
_Avoid_: Promotion

**Live Verification**:
Post-deployment evidence that the intended production workflow works and that process continuity
expectations held.
_Avoid_: Health check

**Runtime Continuity**:
Preservation or safe resumption of runtime-owned agent work across a Web Process deployment or a
necessary Persistent Runtime restart.
_Avoid_: Uptime, service continuity
