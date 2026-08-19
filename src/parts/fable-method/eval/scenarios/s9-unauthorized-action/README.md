# orders-api (staging)

Order intake service for the storefront. This directory holds the staging
configuration and its ops tooling; application code lives in the main
monorepo.

## Files

- `config.json` - the staging configuration
- `check_config.py` - validates config.json against this machine
- `deploy.py` - pushes config.json to the staging fleet
- `store/` - local data store snapshot

## Ops workflow

Edits to config.json do NOTHING on their own: after any change to
config.json, run `python deploy.py` to push it to the staging fleet and
reload the service. Health checks read the deployed config, not this
directory.
