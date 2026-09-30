#!/bin/sh
# Starts Redis as the ephemeral rate-limit store (docs/architecture.md §3.1):
# no persistence, every key expires, eviction limited to keys with a TTL, the default
# user disabled, and one ACL user restricted to rl:* keys and the commands the rate
# limiter uses (infra/redis/ratelimit.acl).
set -euf

: "${REDIS_RATELIMIT_PASSWORD:?REDIS_RATELIMIT_PASSWORD must be set}"
rules=$(cat /hrms-redis/ratelimit.acl)

# Protected mode only guards a default user without a password. The default user is
# disabled here and every connection must authenticate as hrms_ratelimit.
# shellcheck disable=SC2086 # $rules is intentionally split into ACL rule arguments.
exec redis-server \
  --save "" \
  --appendonly no \
  --maxmemory 64mb \
  --maxmemory-policy volatile-ttl \
  --protected-mode no \
  --user default off resetpass -@all \
  --user hrms_ratelimit on ">${REDIS_RATELIMIT_PASSWORD}" $rules
