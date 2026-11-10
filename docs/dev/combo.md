# timu embedded inside another package

## Build the combined package's Anthropic adapter before releasing.

`timu.provider`, `timu.api` and the `event` schema are now declared public, but
nothing outside timu uses them yet. An interface released before its first
consumer usually needs breaking changes soon after. A minimal
`timu_sdk.anthropic:from_settings` would exercise most of it: `Messages` API
streaming, tool use, `Message.extra` for thinking blocks, the cache-token
mapping and `reply_problems`. It will show whether `ProviderSettings`,
`CONTEXT_OVERFLOW` and the `Usage` fields hold up. Expect it to need work on
retry semantics: the SDK's own retries do not match timu's rule of retrying
after streamed text with a restart marker.


