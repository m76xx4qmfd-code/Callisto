# Principal-Bound Kalshi Ledger Correlation Plan

## Objective

Bind new immutable venue order intents to an optional opaque authenticated-principal fingerprint, then let the disconnected Kalshi portfolio coverage sweep classify venue activity as locally known only when exact principal-scoped ledger evidence proves ownership.

This increment adds no route, worker, startup hook, credential loading, socket connection, venue mutation, private-stream health promotion, or retry authorization.

## Durable identity

`venue_order_intents.authenticated_principal_fingerprint` is nullable for existing and paper-only intents. A bound value must be a lowercase 64-character SHA-256. Null is never a wildcard and cannot correlate with an authenticated sweep. Historical rows are not backfilled because the immutable ledger has no trustworthy way to infer their principal.

`VenueIntentProvenance` carries and validates the fingerprint at initial intent persistence. Idempotent replay compares it as an immutable fact. The existing global `(venue, client_order_id)` uniqueness remains deliberately conservative: reusing one client ID under another principal is a hard conflict, never re-attribution.

Alembic revision `202606160006` adds only the nullable column, check constraint, and lookup index. The existing row-level update/delete/truncate immutability trigger remains installed and no historical row is mutated.

## Conservative correlation

After all four venue GET traversals and evidence validation, the service reads local ledger evidence for exactly the client's non-null principal fingerprint.

An observed order is known only when one principal-bound intent and its immutable provider acknowledgement agree on:

- client order ID and provider order ID;
- ticker;
- book side;
- limit order type;
- exact initial quantity; and
- exact effective limit price.

An intent without an acknowledgement remains unknown. An observed fill is known only when its provider fill ID is already bound by an immutable execution event to the exact known intent and provider order. Unbound, cross-principal, cross-intent, mismatched, or missing evidence remains operator-visible as unknown.

The local-ledger read is an attribution snapshot, not proof of local-ledger completeness. A concurrent ledger addition can only leave activity conservatively unknown in the already committed checkpoint. `complete` continues to describe bounded venue traversal and modeled evidence consistency only; unknown activity does not authorize retry and this slice does not restore private WebSocket health.

## Committee disposition

Opus and GLM conditionally approved the direction; Kimi returned an empty provider response. Required changes were adopted:

- acknowledgement is mandatory before an order can be known;
- null fingerprint matching is structurally excluded and the sweep already rejects invalid client fingerprints;
- cross-principal client ID reuse remains a hard conflict under existing uniqueness;
- fill knowledge requires exact event ownership;
- migration behavior is tested against the real immutable table rather than backfilling;
- checkpoint documentation does not claim a transactional local or venue snapshot.

## Verification

Focused PostgreSQL tests cover exact known order/fill attribution, unbound and cross-principal isolation, acknowledgement requirement, cross-intent and cross-order identity mismatch, fill-event ownership, fingerprint validation, immutable replay conflicts, and existing coverage/ledger invariants. Independent final review found that a fill event could suppress the same fill ID observed on a different known order; a RED two-order regression now requires the venue fill `order_id` to equal the event's provider order. Migration verification includes head downgrade/upgrade and empty-database base-to-head replay.
