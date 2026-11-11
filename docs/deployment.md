# CohortLens deployment SuperNodes

This runbook executes the synthetic simulation track through registered `flower-supernode` processes
instead of Flower's Simulation Runtime. Keep private keys, datasets, runtime
state, and logs outside the repository.

## 1. Prepare the three site-local snapshots

For the synthetic deployment demonstration:

```shell
uv run cohortlens prepare-supernode-data \
  --output-dir /PRIVATE/cohortlens-supernodes/data
```

This writes one `.npz` per site and a local manifest. Each archive contains
only the fixed five-column design matrix and outcome array. The ClientApp
rejects archives with additional arrays.

## 2. Register three node identities

Create a distinct key on each machine:

```shell
ssh-keygen -t ecdsa -b 384 -N "" -f /PRIVATE/keys/site-1
flwr supernode register /PRIVATE/keys/site-1.pub supergrid
```

Repeat for sites 2 and 3. Create a deployment federation and add every returned
node ID:

```shell
flwr federation create cohortlens-deployment supergrid \
  --description "CohortLens three-site deployment SuperNodes"
flwr federation add-supernode NODE_ID \
  @ACCOUNT/cohortlens-deployment supergrid
```

Simulation federations cannot accept registered SuperNodes.

## 3. Start one SuperNode per site

Run the following form on each site machine, changing the site number, private
key, snapshot path, runtime directory, and local Runtime API port:

```shell
FLWR_HOME=/PRIVATE/runtime/site-1 uv run flower-supernode \
  --superlink fleet-supergrid.flower.ai:443 \
  --auth-supernode-private-key /PRIVATE/keys/site-1 \
  --node-config 'site-alias="site-1" dataset-path="/PRIVATE/data/site-1.npz" snapshot-alias="deployment-site-1-v1"' \
  --host 127.0.0.1 --port 9094 \
  --allow-runtime-dependency-installation
```

Use different ports when multiple SuperNodes share a host. For actual partner
deployment, each institution should run only its own node and mount only its own
read-only snapshot.

Verify registration and connectivity:

```shell
flwr supernode list supergrid --verbose
flwr federation list supergrid \
  --federation @ACCOUNT/cohortlens-deployment
```

## 4. Run the primary analysis

```shell
uv run cohortlens prepare-synthetic \
  --output-dir runs/phase1-deployment \
  --approved-by OPERATOR --valid-hours 24

uv run flwr run apps/cohort-analytics supergrid \
  --federation @ACCOUNT/cohortlens-deployment \
  --run-config runs/phase1-deployment/analytics-run.toml --stream
```

The result must contain the three configured `deployment-site-*-v1` snapshot
aliases. A `synthetic-site-*-v1` alias indicates that the simulation fallback
was used instead.

Stop a foreground SuperNode with Ctrl+C. For persistent operation, supervise
the same command with the site's normal service manager or scheduler.
