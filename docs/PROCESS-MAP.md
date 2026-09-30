# Process map

Three processes run the accelerator. The first two keep the price book current; the third turns an
intake workbook into a priced comparison. The step-by-step procedures are in
[`OPERATOR-GUIDE.md`](OPERATOR-GUIDE.md); the components are in the README's "Architecture" section.

## 1. Monthly price book loop

Harvesting is automatic. Review, approval, and publishing are signed-in human actions on the web app's
**Price book** page. Nothing publishes on its own.

```mermaid
flowchart TB
    subgraph sched[Harvest schedule: Logic App]
        s1["Day harvestMonthDay, harvestHourUtc<br/>(default day 2, 06:00 UTC)"]
        s2{"VM stopped or<br/>deallocated?"}
        s3["Tag harvest request<br/>and start VM"]
        s4["Poll VM power state every 10 min<br/>until it powers off (up to 6 hours)"]
        s5["Deallocate VM"]
        s6["Read result tag"]
        s7{"VM deallocated and tag says<br/>succeeded for this run?"}
        ok["Run succeeds"]
        alert["Run fails:<br/>failed-harvest alert"]
    end
    subgraph vm[Harvester VM]
        h1["Claim request"]
        hb["Check data disk; fetch and verify<br/>current Published (or note first run)"]
        h2["Collect Azure and AWS<br/>public list prices"]
        h3["Validate the snapshot<br/>(against current Published)"]
        h4["Derive rate extract; diff against<br/>current Published (or packaged<br/>baseline on the first run)"]
        h5["Stage run in staged-runs"]
        h6["Record result tag<br/>and power off"]
    end
    subgraph people[Price book page]
        r1["SkuMapReviewer:<br/>Record SkuMap review"]
        a1["SnapshotApprover:<br/>Approve snapshot"]
        p1["SnapshotApprover:<br/>Publish snapshot"]
    end
    subgraph api[Pricing API]
        v1["List staged runs; check files,<br/>row metadata, and signatures"]
        k1["Hash the full price rows;<br/>sign the review (Key Vault key)"]
        k2["Hash the full price rows;<br/>sign the approval (Key Vault key)"]
        c1["Re-verify, write Published artifact,<br/>move current.json, record publication"]
        e1["New estimates price from<br/>the new Published snapshot"]
    end

    s1 --> s2
    s2 -- no --> alert
    s2 -- yes --> s3 --> h1 --> hb --> h2 --> h3 --> h4 --> h5 --> h6
    s3 --> s4
    h6 -. powers off .-> s4
    s4 --> s5 --> s6 --> s7
    h6 -. result tag .-> s6
    s7 -- yes --> ok
    s7 -- no --> alert
    h5 -. staged run .-> v1
    v1 --> r1 --> k1 --> a1 --> k2 --> p1 --> c1 --> e1
    alert -. operator fixes the cause and reruns now .-> s2
```

Review does not depend on the schedule's result. A run that staged before something later failed (for
example, deallocation or the result tag) still appears on the Price book page; check `staged-runs` after a
failed-harvest alert. A failed or unpublished month does not stop pricing: the current Published snapshot keeps pricing until
its capture date is more than `pricebookMaxAgeDays` old (default 45). The home page flags it as stale
after 30 days.

## 2. Staged run states

The Price book page shows each staged run in one of these states. The API computes the state from
verified records; the browser only displays it.

```mermaid
stateDiagram-v2
    [*] --> AwaitingSkuMapReview: harvester stages a run
    AwaitingSkuMapReview --> AwaitingApproval: SkuMapReviewer records review
    AwaitingApproval --> Approved: SnapshotApprover approves
    Approved --> Published: SnapshotApprover publishes
    AwaitingSkuMapReview --> Blocked: a check fails
    AwaitingApproval --> Blocked: a check fails
    Approved --> Blocked: a check fails
    Published --> Blocked: a check fails
    note right of Blocked
        Files, digests, or signatures do not verify.
        A reviewed but unapproved run is also Blocked
        if the API's SkuMap changed since the review.
        The API lists the problems.
    end note
    note right of Published
        A later publication makes this run
        no longer current; it stays Published.
    end note
```

A SkuMap change does not move Approved or Published runs to Blocked, but the pricing engine refuses a
Published snapshot approved against a different SkuMap, so estimates return 503 until a snapshot approved
against the current SkuMap is Published. One person may hold both sign-off roles; the review and approval are still recorded separately. If
publishing is interrupted after `current.json` moved, the run reports that publishing did not finish and
pricing stops until **Publish snapshot** is selected again on the same run.

## 3. Estimate flow

An Estimator turns an intake workbook into a comparison. Every number comes from the API, computed with
`decimal` and stamped with a run hash.

```mermaid
flowchart TB
    m1["New estimate: choose mode<br/>(Azure, AWS, or both)<br/>and confirm the region pair"]
    u1["Upload intake workbook (.xlsx)"]
    i1{"Intake checks pass?<br/>size, OpenXML package,<br/>no macros, formulas, external links,<br/>encryption, DTDs, unsafe compression"}
    x1["Rejected with a reason<br/>(legacy .xls: Save As .xlsx)"]
    n1["Normalize to cloud-neutral units;<br/>open Gaps for missing facts"]
    src{"PRICEBOOK_SOURCE"}
    p0{"Current Published snapshot<br/>valid and within max age?"}
    x2["503: pricing unavailable"]
    p1["Price each unit as multiple meters per cloud<br/>with the approved SkuMap; missing facts are<br/>Unpriced or approved Exclusions, never zero"]
    g1{"CompletenessGate:<br/>no open material Gaps, no Unpriced lines,<br/>no license question that blocks pricing,<br/>both totals present?"}
    d1["DraftBenchmark:<br/>headline held, open questions shown"]
    q1["Estimator answers a Gap<br/>(recorded with Confirmed by)"]
    r1["ReviewBaseline:<br/>summary, totals, Not included list,<br/>scope, confidence, breakeven"]
    e1["Export: printable summary,<br/>priced workbook, evidence JSON"]
    t1["Estimate expires after<br/>APPLICATION_MAX_AGE_HOURS (default 1 hour);<br/>the API discards it"]

    m1 --> u1 --> i1
    i1 -- no --> x1
    i1 -- yes --> n1 --> src
    src -- published-blob<br/>(deployed default) --> p0
    src -- demo-extract<br/>(local default) --> p1
    p0 -- no --> x2
    p0 -- yes --> p1 --> g1
    g1 -- no --> d1 --> q1 --> p1
    g1 -- yes --> r1 --> e1
    e1 -.-> t1
    d1 -.-> t1
```

The priced workbook and evidence JSON export only at ReviewBaseline. Estimates are held in memory, so
download both files before the estimate expires. The browser may still list an expired estimate by name,
but its workbook and comparison are gone.
