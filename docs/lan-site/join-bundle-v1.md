# Join file v1

A join file is a sensitive, short-lived enrollment artifact. It is not a software bundle and it is not service discovery. Software, models and pinned release assets continue to use the existing offline bundle.

## JSON shape

```json
{
  "version": 1,
  "site_id": "clfs-pilot",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/BASE64_OF_32_BYTES"],
  "enrollment_token": "one-use-secret",
  "mdns_service": null
}
```

The decoder rejects unknown fields.

- `version` must be `1`.
- `site_id` is a non-empty operator-chosen identifier.
- `coordinator_urls` contains one or more HTTPS origins. Entries have no user information, query, fragment or non-root path. Order is significant.
- `coordinator_spki_sha256` contains one or more standard-base64 SHA-256 digests of the coordinator certificate's DER-encoded SubjectPublicKeyInfo. This permits a planned current/next key transition.
- `enrollment_token` is a non-empty token minted by the existing enrollment-token store. It is consumed once and never logged.
- `mdns_service` is either `null` or `_fallow._tcp.local.`. It is ignored until optional discovery support is installed.

## TLS verification

For every connection, the Go agent hashes `PeerCertificates[0].RawSubjectPublicKeyInfo` and compares it in constant time with the configured pin set. It also checks the certificate validity window. The URL host and port choose where to dial; they do not establish trust.

The agent verifies TLS before writing an Authorization header, enrollment token or request body. Site clients do not follow redirects and do not use environment, WinHTTP, PAC or WPAD proxy settings. A wrong pin is a hard failure.

The initial certificate should have a long pilot lifetime. The operator should provision both current and next pins before changing keys. If the active key is lost or no trusted pin remains, the recovery path is to distribute a new join file locally. There is no network trust reset.

## Persistence

After registration succeeds and the identity file is durable, the agent stores a token-free site profile alongside its `agent_id` and `device_token`. It then removes the installed copy of `enrollment_token`. Existing two-field identities continue to load as legacy identities.

A supplied join file never replaces an existing identity silently. If registration might have reached the coordinator but its response was lost, the agent reports an ambiguous enrollment and asks for a new token instead of retrying.

Before the school test, IT must confirm that the identity and token-free profile survive reboot, profile cleanup and any reimaging product such as Deep Freeze. If that storage is not persistent, enrollment design must be revisited before the pilot.
