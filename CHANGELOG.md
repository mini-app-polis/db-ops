## [1.3.2](https://github.com/mini-app-polis/db-ops/compare/v1.3.1...v1.3.2) (2026-10-15)


### Bug Fixes

* evaluator findings on db-ops (docstring, idempotent canary, no-retry note, CD-009 deferral) ([5176d18](https://github.com/mini-app-polis/db-ops/commit/5176d188fae556008ef93adf7c8ff598c4813322))

## [1.3.1](https://github.com/mini-app-polis/db-ops/compare/v1.3.0...v1.3.1) (2026-10-15)


### Bug Fixes

* **drill:** confirm teardown by reading Railway back, and give deletes time to answer ([f64c393](https://github.com/mini-app-polis/db-ops/commit/f64c393ffaa72cc835712b75e504c7bb75378033))

# [1.3.0](https://github.com/mini-app-polis/db-ops/compare/v1.2.0...v1.3.0) (2026-10-15)


### Features

* **drill:** monthly PITR restore drill with measured RPO and RTO ([c36ba71](https://github.com/mini-app-polis/db-ops/commit/c36ba7119ceeada450b5ebfbd39bf0b2d9721229))

# [1.2.0](https://github.com/mini-app-polis/db-ops/compare/v1.1.1...v1.2.0) (2026-10-15)


### Bug Fixes

* **backup:** warn when Healthchecks rejects a ping, and keep request URLs out of the logs ([a0acd9b](https://github.com/mini-app-polis/db-ops/commit/a0acd9b895ef0539985715b5b8cc10f0a1d18e29))


### Features

* **poller:** publish both databases' Postgres metrics to CloudWatch ([2181047](https://github.com/mini-app-polis/db-ops/commit/2181047d5d616cc43f961fa4a62b08d083be08b7))

## [1.1.1](https://github.com/mini-app-polis/db-ops/compare/v1.1.0...v1.1.1) (2026-10-15)


### Bug Fixes

* **backup:** declare settings with pydantic-settings and move the lock-wait timeout into them ([4c0ef1f](https://github.com/mini-app-polis/db-ops/commit/4c0ef1fa974b9be2c1484a7e4203190b3b74c5bf))

# [1.1.0](https://github.com/mini-app-polis/db-ops/compare/v1.0.1...v1.1.0) (2026-10-15)


### Features

* **backup:** nightly pg_dump of both databases to S3 ([ba579cb](https://github.com/mini-app-polis/db-ops/commit/ba579cbe48f6c2bd60919356ef445d186c1efdb3))

## [1.0.1](https://github.com/mini-app-polis/db-ops/compare/v1.0.0...v1.0.1) (2026-10-15)


### Bug Fixes

* triage first evaluation — docstrings, respx, PY-013 exemption ([9debc64](https://github.com/mini-app-polis/db-ops/commit/9debc6482db9722d41918ca374c2f0eab5421f3a))

# 1.0.0 (2026-10-15)


### Features

* scaffold db-ops with db-poller, db-backup and db-drill entry points ([cdd3825](https://github.com/mini-app-polis/db-ops/commit/cdd3825bd5db0ffc370944caebb477a534c12e82))
