# Recommendation: Warehouse Resize

Recommend resizing the warehouse when the query spills memory to disk.
Spilling is a strong, unambiguous signal — once present, the query is
paging working data through the warehouse's local SSD (or, worse, remote
storage) on every operator that doesn't fit. A larger warehouse has more
memory and usually eliminates the spill.

## When to recommend

- `bytes_spilled_to_local_storage > 0` **or**
  `bytes_spilled_to_remote_storage > 0` (the candidate has
  `warehouse_size_opportunity = 1`).
- The EXPLAIN plan contains an `Aggregate`, `Sort`, or `WindowFunction`
  operator processing a large input — confirming the spill came from a
  memory-pressure operator and not, say, from an oversized intermediate
  result that better SQL could shrink.

If spill is present but the plan shows no aggregation/sort/window — for
instance, a plain large-table scan — prefer Query Acceleration Service
instead. Spill in a plain scan is unusual; it usually means the warehouse
is just too small for the result-set materialization.

## What to recommend (`recommendation_target`)

The next size up from the candidate's current `warehouse_size`:

```
X-Small  →  Small      (1 → 2 credits/hr)
Small    →  Medium     (2 → 4)
Medium   →  Large      (4 → 8)
Large    →  X-Large    (8 → 16)
X-Large  →  2X-Large  (16 → 32)
2X-Large →  3X-Large  (32 → 64)
... and so on, doubling each step
```

Format: `"<current> → <next>"` (e.g. `"Medium → Large"`).

Default is one size up — each step doubles credits/hour, so skipping
levels wastes money when a single step would have sufficed. If
`bytes_spilled_to_remote_storage` is large (rough threshold:
`> bytes_scanned * 0.5`, i.e. half the working set spilled all the way
to remote), recommend two sizes up and note that in the rationale.

## Spill severity affects rationale tone

| Spill type | Meaning | Rationale tone |
|---|---|---|
| Only `bytes_spilled_to_local_storage > 0` | Suboptimal but workable; some local spill is normal on borderline-sized warehouses. | "Local spill of N MB on `<size>` — query would benefit from more memory." |
| `bytes_spilled_to_remote_storage > 0` | Bad; even local SSD wasn't enough, the query is paging to remote storage. | "Remote spill of N MB on `<size>` — query is paging to remote storage and is significantly slower than it needs to be." |

## Caveats to note in `rationale`

- **Cost**: a resize doubles credits per unit time. The recommendation is
  worth it when the query runs frequently or its current elapsed is
  significant. For a one-off query, mention that the resize cost may
  exceed the benefit.
- **Resize is persistent**: it stays in effect until the operator changes
  it back. Don't suggest "just for this query" — that's not a resize, it's
  a separate warehouse, which is a different recommendation.
- **Account cap**: if the candidate is already at the largest size the
  account permits (commonly 6X-Large), there's no resize available;
  recommend Query Acceleration Service or `recommendation_type: "none"`
  with that explanation.

## Why this isn't auto-tested

The plugin's allowlist hook permits `ALTER WAREHOUSE … SUSPEND/RESUME` but
**not** `ALTER WAREHOUSE … SET WAREHOUSE_SIZE`. That's deliberate:

- A resize persists after the run, which is stateful and error-prone to
  roll back ("back to what?" depends on the size at session start).
- Operators want to think about cost before changing warehouse sizes —
  auto-resizing on their behalf violates "the plugin proposes, the
  operator decides."

So this is a recommendation only. The operator runs the resize themselves
once if they agree.
