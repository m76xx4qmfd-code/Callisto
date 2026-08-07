# Kalshi public-result evidence canonicalization

## Scope

This contract applies only to Callisto's **strict selected settlement projection**
of the public Kalshi `Market` response. It is not full validation of every
`Market` property. Canonical decimal spelling is a local fail-closed evidence
policy, **not an OpenAPI invariant**.

The selected evidence object contains the validated ticker, event ticker,
market type, lifecycle status, result, one-dollar notional, required
`expiration_value`, source origin/path, source `Date` instant, and the separately
pinned resolution OpenAPI version/hash. Optional `settlement_ts` and
`settlement_value_dollars` retain wire presence independently.

## Exact bytes

Canonical bytes are produced as follows:

1. Encode one JSON object as **UTF-8**.
2. Emit object keys in **lexicographically sorted** order.
3. Emit no whitespace, with separators exactly `,` and `:`.
4. Enable JSON **ASCII escaping** (`ensure_ascii=True`).
5. Omit absent optional keys.
6. Encode an explicitly present null as the JSON token **explicit null**
   (`null`).
7. Encode valid decimal financial values as canonical strings at six decimal
   places. No JSON numeric float is permitted.
8. Preserve required empty `expiration_value` as `""`.
9. Compute lowercase SHA-256 over those exact UTF-8 bytes.

The object uses `observed_at` for the provider's accepted response `Date`.
`fetched_at` remains correlated response metadata but is not part of the
provider evidence identity. A response is accepted only when
`-5 <= (fetched_at - observed_at).total_seconds() <= 5`.

## Presence examples

- If `settlement_ts` is absent, the evidence object omits it.
- If `settlement_ts` is present as null, evidence contains
  `"settlement_ts":null`.
- If present as a valid timestamp, evidence contains its normalized UTC
  RFC3339 string.
- `settlement_value_dollars` follows the same absent/null/value rule, with a
  valid value emitted as a canonical decimal string.

Tests construct reference bytes independently as a byte literal before hashing;
they do not validate the serializer by calling the serializer a second time.
