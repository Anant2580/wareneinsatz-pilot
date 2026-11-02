#!/bin/sh
# One-off job before the API starts (compose service "setup"). Safe to run again: every step
# skips what is already there, so restarts and updates never duplicate data.
#
#   1. database migrations
#   2. ingredient master list and recipes (added or updated from the files in data/)
#   3. 26 weeks of made-up purchases from five fictional suppliers (only once)
#   4. one invoice with a misprint, so the review screen has something to show
#   5. ingredient suggestions for its lines, and the price / target alerts
set -eu

alembic upgrade head
wareneinsatz ingredients load
wareneinsatz recipes load
wareneinsatz demo load
wareneinsatz ingest data/demo/*.xml
wareneinsatz match --no-embeddings
wareneinsatz alerts > /dev/null
echo "Setup done."
