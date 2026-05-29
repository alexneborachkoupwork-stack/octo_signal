# Production-Grade Workflow & Real-Time Slot Orchestration Refactor

## Overview

This document defines the finalized production-grade architecture for a distributed appointment automation platform designed for:

- 200+ concurrent sessions
- scarce and highly competitive slot availability
- resumable workflows
- real-time slot coordination
- proxy-aware recovery
- low-latency booking execution

The system is designed around:

- deterministic state machines
- asynchronous manager coordination
- probabilistic slot intelligence
- late slot assignment
- fallback slot consumption
- production-grade observability

---

## High-Level Architecture

```text
Manager (1)
↕
Sessions / Workers (N)
```

### Rules

- Sessions communicate ONLY with manager.
- Sessions NEVER communicate with each other.
- Manager is coordination/orchestration layer only.
- Target backend owns slot inventory.
- Slot availability is probabilistic and changes in real time.

---

## Core Design Principles

### 1. Non-blocking execution

Workers must never stop workflow execution waiting for manager responses.

Manager communication must be:

- async
- background
- non-blocking

Workers continue progressing independently.

### 2. Deterministic state machine

Every workflow must be resumable.

Each worker must persist:

```ts
{
  workflow,
  state,
  page,
  accountId,
  profileId,
  proxyId,
  retryCount,
  lastAction,
  timestamp
}
```

Recovery supported for:

- browser crash
- proxy disconnect
- refresh
- extension reload
- login expiration
- session disconnect

---

## Workflow Overview

1. Register
2. Warmup
3. Apply
4. Signal
5. Test All-In-One

---

## Register Workflow

### Step 0 — Initialization

Options:

- Create profile + assign proxy
- Rotate proxy only

Failures:

- timeout
- proxy issue
- connection issue

Recovery:

```text
rotate proxy
```

Retry:

```ts
maxProxyRetry = 9
```

### Step 1 — Registration

Failure:

```text
registration failed
```

Recovery:

```text
change profile + rotate proxy
```

Retry:

```ts
maxRegistrationRetry = 3
```

### Step 2 — Idle

Duration:

```text
1 minute
```

### Step 3 — Login

Failure:

```text
login failed
```

Recovery:

```text
sleep → rotate proxy → retry
```

Example backoff:

```text
5 min → 10 min → 20 min
```

Retry:

```ts
maxLoginRetry = 3
```

### Step 4 — Index Idle

```ts
if (warmup) {
  startWarmup()
} else {
  saveProfile()
}
```

---

## Warmup Workflow

### Step 0 — Session bootstrap

Cases:

- launched + connected → continue
- launched + disconnected → reconnect
- browser closed → launch
- none → create + launch

### Step 1 — Login

Use Register login retry logic.

### Step 2 — Await index ready

Expected:

```text
APPLY FOR A VISA
```

Timeout:

```text
refresh page
```

### Step 3 — Navigate Questionario

Recover from:

- timeout
- redirect
- selector missing

Action:

```text
refresh
detect current step
continue
```

### Step 4 — Fill form

Fill all fields except:

```text
consular post
```

### Step 5 — Consular Post

Validate:

- required fields complete
- validator passes
- selected post equals target

Success:

```text
go Apply workflow
```

Failure:

```text
go Idle mode
```

### Step 6 — Idle Mode

Behavior:

- remain on form page
- monitor slot changes
- refresh periodically
- continue reporting

Connection lost:

```text
refresh
resume
```

Logged out:

```text
restart login
```

---

## Real-Time Slot Intelligence Pool

Manager maintains:

```ts
slotPool
```

This is:

```text
best-known real-time slot intelligence
```

NOT guaranteed inventory.

### Slot entry structure

```ts
type SlotPoolEntry = {
  slotKey: string
  postId: number
  date: string
  time: string

  status:
    | "available"
    | "consuming"
    | "likely_consumed"
    | "consumed"
    | "stale"
    | "unknown"

  confidenceScore: number

  observedCount: number
  observedBy: string[]

  assignedSessions: string[]

  lastObservedAt: number
  lastAssignedAt?: number

  successCount: number
  failureCount: number

  version: number
}
```

---

## Earliest Slot Detection

The system must discover the earliest reliable timing for slot visibility.

Potential sources:

- XHR interception
- fetch interception
- hidden API payloads
- DOM rendering
- JS memory objects
- schedule endpoint responses

Goal:

```text
detect slots ASAP
preferably before UI render
```

---

## Slot Observation Pipeline

Whenever worker sees slot data:

Immediately report:

```ts
reportSlotObservation({
  workerId,
  accountId,
  page,
  timestamp,
  slots
})
```

Worker never waits.

Execution continues.

---

## Slot Pool Update Logic

### New observation

```text
available
confidence++
```

### Missing slot

Decay confidence.

Transition:

```text
available → stale → likely_consumed
```

### Successful booking

```text
status = consumed
```

### Failed booking

Examples:

```text
already taken
invalid slot
slot unavailable
```

Immediately report:

```ts
reportSlotFailure({
  slotKey,
  reason
})
```

---

## Critical Strategy — Late Slot Assignment

DO NOT reserve slots early.

Assignment occurs ONLY when worker reaches:

```text
slot selection step
```

Benefits:

- reduced stale ownership
- better pool efficiency
- lower slot waste

---

## Slot Assignment Flow

Worker requests:

```ts
requestSlotAssignment({
  workerId,
  accountId,
  postId,
  visibleSlots,
  timestamp
})
```

Manager returns:

```ts
{
  primary,
  fallbacks,
  version
}
```

---

## Fallback Strategy

Why needed:

Between:

```text
assignment → submit
```

slot may disappear.

Worker instantly tries:

```text
primary
→ fallback #1
→ fallback #2
→ fallback #3
```

No manager roundtrip.

Fallback switching is:

```text
instant
local
low latency
```

Fallbacks may overlap across workers.

Backend owns truth.

---

## Dynamic Fallback Refresh

If booking latency is long:

Refresh fallback chain dynamically.

Potential frequency:

```text
1 second
```

during contention periods.

---

## Apply Workflow

### Step 0 — Start

Click submit.

### Step 1 — Early slot monitoring

Begin monitoring ASAP using:

- XHR hooks
- fetch hooks
- DOM observers
- network inspection

### Step 2 — Request assignment

At selection time:

Request:

```text
primary + fallback slots
```

### Step 3 — Solve reCAPTCHA

Only AFTER assignment.

### Step 4 — Consume slot

Attempt:

```text
primary
```

Failure:

```text
fallback chain
```

### Step 5 — Submit

Submit immediately.

Avoid:

- waits
- heavy logs
- unnecessary DOM operations

### Step 6 — Download PDF

Validate successful download.

---

## Signal Workflow

Purpose:

Resume workers.

Detect:

- workflow
- state
- page
- auth status

Resume exact step.

---

## Test All-In-One

```text
Register
→ Sleep (<2 min)
→ Warmup
→ Idle
→ Signal
→ Apply
```

Must support:

- integration tests
- concurrency simulation
- recovery testing
- slot race testing

---

## Refresh Strategy

Avoid:

```text
all workers refresh together
```

Use jitter:

```ts
refreshInterval =
  base + random(0–15s)
```

Near selection:

```text
refresh faster
```

Idle:

```text
refresh slower
```

---

## Observability

Manager tracks:

```ts
{
  activeWorkers,
  warmedWorkers,
  selectingWorkers,

  slotPoolSize,
  confidenceDistribution,

  successfulBookings,
  failedBookings,

  fallbackUsageRate,
  collisionRate,

  avgSubmitLatency,
  recaptchaUsage,

  proxyHealth,
  workflowDistribution
}
```

---

## Engineering Requirements

1. Deterministic state machine
2. Resume-safe workflows
3. Event-driven orchestration
4. Persistent state storage
5. Structured logging
6. Async communication
7. Real-time slot intelligence
8. Dynamic fallback orchestration
9. Production retry handling
10. Proxy-aware recovery
11. Low-latency critical path
12. Fault tolerance
13. Distributed coordination safety
14. Modular architecture
15. Full observability

---

## Final Objective

Optimize:

```text
successful bookings per minute
```

Minimize:

```text
race loss
slot waste
captcha waste
```

Maximize:

```text
submit success rate
low latency booking
system resilience
```
