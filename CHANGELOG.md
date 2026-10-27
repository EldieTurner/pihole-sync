# Changelog

## 0.2.2

- Publish the image to `eldieturner/pihole-sync` on Docker Hub for AMD64,
  ARM64, and ARMv7, with release, minor-series, and latest tags.
- Add a Docker Hub overview, public pull-based Compose setup, separate
  source-build Compose file, and release-publishing support.
- Include the MIT license and public reference/configuration examples in
  the image. Live credentials and state remain outside the build context.
- Preserve the 0.2.1 API compatibility fix and synchronization behavior.

## 0.2.1

- Supply the list type query parameter required by Pi-hole FTL 6.8.
- Add regression coverage for block-list and allow-list creation.
- Document Docker-managed DNS exclusions and provide sanitized deployment
  configuration for the primary/replica pair.

## 0.2.0

- Synchronize Pi-hole v6 configuration from a primary to replicas through
  the REST API, with a dashboard, persistent snapshots, and offline tests.
