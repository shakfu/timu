# TODO

## Critical

## High

## Medium

## Low

- Remove copies under `~/.local/state/timu/work/` automatically, or warn when they pile up. Each `timu run` now makes one; only `timu clean` removes them.
- Local `make test` skips the bwrap tests on a default Ubuntu 24.04 host (AppArmor blocks user namespaces). Only CI catches a bwrap regression. Decide whether Linux is first-class and, if so, how to run them locally.
- Decide whether graph Refs may set `report_path`. The path now gets the `.git` and PROTECTED checks, but an upstream coder still picks it.
- Run timu as an MCP server once a caller cannot spawn the CLI, e.g. a remote or sandboxed agent. Until then `timu run` suffices: pma launches agents as command records. MCP over stdio is JSON-RPC, which the stdlib handles, so the no-dependency rule holds.
