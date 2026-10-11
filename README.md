# feature-flag-service

A feature flag API: create flags, roll them out to a percentage of users or to users matching
rules, evaluate them, and keep an audit trail of every change. It is the first application of
the IDP and the service every other service on the platform asks "is this feature on?".

The service is intentionally modest. The platform around it is the point: this repository is
where each platform capability was first tried on a real application.

It is one of five repositories:

| Repository | Role |
|---|---|
| [idp-infra](https://github.com/surya-idp/idp-infra) | Terraform for AWS, `make up` / `make down`, the end-to-end test, the project's master documents |
| [idp-gitops](https://github.com/surya-idp/idp-gitops) | What runs in the cluster; this service's deploy files are in `apps/feature-flag-service` |
| [idp-portal](https://github.com/surya-idp/idp-portal) | Backstage; its "Add a feature flag" template calls this API |
| [idp-platform](https://github.com/surya-idp/idp-platform) | The CI workflow template services call |
| **feature-flag-service** (this one) | The flag API |

## The API

| Method | Path | Who | What |
|---|---|---|---|
| `POST` | `/auth/login` | anyone | Username and password -> a token valid for 30 minutes |
| `GET` | `/health` | anyone | 200 when the database answers, 503 when it does not |
| `POST` | `/flags` | admin | Create a flag |
| `GET` | `/flags` | signed in | List flags (`?environment=`, `?flag_type=`) |
| `GET` | `/flags/{id}` | signed in | One flag |
| `PATCH` | `/flags/{id}` | admin | Change a flag. The result must still be a valid flag, or 422 |
| `DELETE` | `/flags/{id}` | admin | Delete a flag (its audit history stays) |
| `GET` | `/flags/{id}/history` | signed in | Audit history, newest first |
| `POST` | `/flags/{id}/rules` | admin | Add a targeting rule |
| `DELETE` | `/flags/{id}/rules/{rule_id}` | admin | Remove a targeting rule |
| `POST` | `/evaluate` | signed in | Is this flag on for this user? Always answers 200 |
| `GET` | `/metrics` | none | Prometheus metrics (reachable only inside the cluster) |

Interactive documentation is served at `/docs`.

### Flag types

| `flag_type` | `rollout_percentage` | How it is evaluated |
|---|---|---|
| `boolean` | must be absent | `default_value` for everyone |
| `percentage` | required, 0 to 100 | The user is placed in one of 100 buckets by a hash of flag name and user id. The same user always lands in the same bucket, on every replica and after every restart |
| `targeted` | must be absent | The first matching rule wins (`equals`, `not_equals`, `in` on a user attribute); otherwise `default_value` |

A flag is unique per `(name, environment)`. Environments are `dev`, `staging`, `prod`.

### `/evaluate` never fails its caller

A service that asks about a flag must never have to handle "the flag service had an error".
Three failure modes, handled differently on purpose:

| What happened | Answer |
|---|---|
| The flag does not exist | 200, `enabled: false`, `reason: flag_not_found`. Not a 404: a missing flag is not the caller's error |
| The cache is down, the database is up | The real answer, from the database. This is not a failure |
| The database is down too, or anything unexpected | 200, `enabled: <default_value>`, `reason: evaluation_error_fail_safe` |

### Who the caller is

Two kinds of token are accepted as `Authorization: Bearer <token>`:

| Caller | Token | Role |
|---|---|---|
| A person | From `POST /auth/login`, signed by this service | The role stored with the user: `admin` or `viewer` |
| A service in the cluster | Its Kubernetes ServiceAccount token, issued for the audience `feature-flag-api` | `admin` if its name is listed in `KUBE_ADMIN_SERVICEACCOUNTS`, otherwise `viewer` |

For a ServiceAccount token this service asks Kubernetes "is this token genuine, was it issued
for me, and whose is it?" (the TokenReview API, `app/kube_identity.py`). Nobody creates, stores
or rotates a secret for it. A token issued for a different audience (for example a pod's
general API token), a person's or a node's token, and any failure to reach Kubernetes are a
401, never a 500. A verified token is remembered for 60 seconds, by its hash.

Template services use this to evaluate flags; Backstage uses it to create them.

### Other behaviour worth knowing

- **Audit log.** Every create, update and delete writes an `audit_log` row in the same
  transaction as the change: action, actor, the flag before and after. The actor is stored as
  text, so history survives the deletion of the user, and `flag_id` becomes NULL when the flag
  is deleted, so history survives that too.
- **Errors.** 401 unknown caller (with `WWW-Authenticate: Bearer`), 403 known but not allowed,
  404 not found, 409 duplicate `(name, environment)`, 422 invalid input or a change that
  would make the flag inconsistent.
- **Metrics.** `flag_evaluations_total{reason, environment}`,
  `flag_evaluations_cache_result_total{result}`, `flag_evaluation_duration_seconds`,
  `flag_mutations_total{action}`.

## Run it locally

You need Docker (for a throwaway database and cache) and Python 3.12 or newer. This sequence
was run as written on 2026-10-11:

```bash
docker run -d --name flags-db    -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16
docker run -d --name flags-cache -p 6379:6379 redis:7-alpine

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export DATABASE_URL=postgresql://postgres:dev@localhost:5432/postgres   # no sslmode: the local database has no TLS
export REDIS_URL=redis://localhost:6379
export JWT_SECRET_KEY=any-long-random-string

python -m alembic upgrade head                                  # creates the tables
python scripts/create_admin.py --username alice                 # asks for a password
python -m uvicorn app.main:app --reload                         # http://localhost:8000/docs
```

Then, in another terminal:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login -d 'username=alice&password=<the password>' \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -s -X POST localhost:8000/flags -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"new-checkout","flag_type":"percentage","default_value":false,"rollout_percentage":25,"environment":"dev"}'

curl -s -X POST localhost:8000/evaluate -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"flag_name":"new-checkout","user_id":"user-1","environment":"dev"}'
# {"flag_name":"new-checkout","user_id":"user-1","environment":"dev","enabled":false,"reason":"percentage_rollout"}
```

Clean up with `docker rm -f flags-db flags-cache`.

`docker compose up --build` is the other way: it builds the image and starts it with a cache,
reading `DATABASE_URL` and `JWT_SECRET_KEY` from `.env` (copy `.env.example`); the database must
be reachable from inside the container.

If the folder of a virtual environment is ever renamed, its commands (`alembic`, `uvicorn`)
stop working, because each one records the old path. `python -m alembic` and
`python -m uvicorn` keep working; recreating the environment fixes it for good.

### Settings

All from environment variables (`app/config.py`). The first three are required; the process
refuses to start without them.

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | The cache; `rediss://` for TLS |
| `JWT_SECRET_KEY` | Signs the login tokens |
| `JWT_EXPIRE_MINUTES` (30), `JWT_ALGORITHM` (HS256), `CACHE_TTL_SECONDS` (10), `LOG_LEVEL` (INFO) | Optional |
| `KUBE_IDENTITY` (`auto`) | `auto`: accept ServiceAccount tokens when running in a pod. `on` / `off` force it |
| `KUBE_TOKEN_AUDIENCE` (`feature-flag-api`) | The audience a caller's token must be issued for |
| `KUBE_ADMIN_SERVICEACCOUNTS` | Comma-separated `system:serviceaccount:<namespace>:<name>` that get the admin role |

### Tests

```bash
pytest tests/ -v
```

About 110 tests. Most need no running database or cache, only the three required variables
set to any well-formed value: the API tests use an in-memory database, and the identity tests
send the real HTTP request to a small local server that plays the Kubernetes API.
`test_metrics.py` needs a reachable PostgreSQL and some cache tests are skipped without a cache;
CI starts both.

## How it is built and deployed

```
push to main ─> CI (.github/workflows/ci.yml)
                  test            pytest against a fresh PostgreSQL and Redis, after a real
                                  Alembic migration from an empty database
                  build-and-push  build the arm64 image, scan it with Trivy, then push it to
                                  ECR feature-flag-service:<commit SHA>
             ─> Argo CD Image Updater (in the cluster) commits the new tag to
                idp-gitops/apps/feature-flag-service/overlays/dev
             ─> Argo CD runs the migration Job, then rolls the Deployment out with no
                dropped requests
```

- **No deploy credential in this repository.** CI logs in to AWS with OIDC and only pushes an
  image.
- **Scan, then push.** Whatever is in ECR gets deployed, so an image with a HIGH or CRITICAL
  finding that has a fix never reaches ECR.
- **Only the newest commit of main is pushed.** Image Updater deploys the image built last.
  Builds run one at a time, and a build whose commit is no longer the head of main skips the
  push, so the older of two quick merges can never end up deployed.
- The image is two-stage, runs as a non-root user, and keeps connections alive for 75 seconds,
  longer than the load balancer's 60-second idle timeout, so the load balancer never reuses a
  connection the server has just closed.

In the cluster (namespace `feature-flag-dev`):

| Need | Where it comes from |
|---|---|
| Database | Its own database `feature_flags` and login on the shared RDS instance, created by the database operator. Not the master user |
| Cache | ElastiCache Valkey over TLS, shared by all replicas |
| Secrets | AWS Secrets Manager -> External Secrets -> the pod's environment. None in git |
| Schema | A migration Job (`alembic upgrade head`) that Argo CD runs before the Deployment |
| Traffic | A route on the platform's shared load balancer; two to four replicas (autoscaled on CPU), a disruption budget, rolling updates that wait for the load balancer |
| Identity | Its own ServiceAccount, allowed to create TokenReviews and nothing else |
| Who may connect | The load balancer, Prometheus, its own namespace, template services and Backstage (NetworkPolicy) |

## Engineering decisions that still hold

**A shared cache, not one per process.** In-memory caching is fine for one replica. With
several, each pod's cache could disagree with the others after a flag change. One shared cache
gives every replica the same view. Proven, not argued: 20 `/evaluate` calls for the same user
and flag through real load balancing returned identical results while the logs showed both
replicas serving.

**A deterministic hash for rollouts.** Python's built-in `hash()` is randomised per process, so
two replicas would put the same user in different buckets. `md5` of flag name and user id is
stable across processes and restarts. It is not used for security.

**Startup failure is not a readiness failure.** With a broken `DATABASE_URL` the pod went to
`CrashLoopBackOff`, not "0/1 Ready": the startup check fails before any probe runs. The probe
thresholds only govern a database that fails on an already running pod. What mattered held
either way: the rolling update kept the healthy pods serving.

**Measure the requirement.** The spec said "evaluation under 50 ms". Nobody had tested it. With
metrics wired up the 95th percentile was about 970 ms: every request was slow, the cache took
0.6 ms and the database fetch about 750 ms. The database was then a hosted PostgreSQL reached
over the internet. Moving it into the same network as the application (RDS in the VPC), with
the same code, brought the 95th percentile to about 20 ms. One variable changed; the number is
in `idp-infra/HANDOVER_DOC.md` section 16a.

**Tests found a real bug.** Writing the API tests showed that a PATCH which made a flag
inconsistent answered 500. It is a 422 with the reason now.

## History

This repository started on 2026-09-12 as an application on a laptop. The stages:

| Stage | Where it ran | What changed |
|---|---|---|
| Steps 1-9 | Docker Compose | Schema and migrations, health, login, flags, evaluation, cache, image, first CI (images to GHCR) |
| Steps 10-16 | A local kind cluster, database hosted outside (Neon) | Probes, Terraform, a separate deploy repository, Argo CD, Backstage catalog, a first Kyverno policy, Prometheus and Grafana |
| Steps 17-28 | AWS: EKS, RDS, ElastiCache | CI to ECR with OIDC, secrets from Secrets Manager, a shared load balancer, zero-downtime rollouts proven under load |
| 2026-10-10 and 11 | The finished platform | Its own database login, API tests, ServiceAccount identity, delivery through Image Updater |

Two bugs from the early stages are worth keeping as lessons, although the files involved no
longer exist:

- **A reference file was applied as a real Secret.** A `secret-template.yaml` with placeholder
  values, kept "for documentation", was picked up by Argo CD and overwrote the working Secret.
  Argo CD applies any valid manifest in its path, whatever the file name suggests. Today no
  Secret is in git in any form.
- **A policy that blocked nothing.** The first Kyverno policy used optional-match syntax, so a
  pod with no security settings at all passed. It was caught by testing the rejection, not by
  reading the policy. Today every policy has a test with a manifest it must reject.

The README of those stages, with the step-by-step kind walkthrough, is in git:
`git show c357524:README.md`.

## Layout

```
app/
  main.py            entry point, startup check, routers
  config.py          settings from the environment
  database.py        engine and sessions
  models.py          tables: users, flags, targeting_rules, audit_log
  schemas.py         request and response shapes, cross-field validation
  auth.py            password hashing, login tokens
  kube_identity.py   Kubernetes ServiceAccount tokens (TokenReview)
  dependencies.py    who is calling, and may they
  evaluation.py      the evaluation rules and the bucket hash
  cache.py           the cache layer and its fallbacks
  audit.py           audit rows
  metrics.py         the four metrics
  routers/           auth, health, flags, evaluate, metrics
alembic/             migrations
tests/               auth, flags API, identity, evaluation, cache, metrics
scripts/             create_admin.py, reset_admin_password.py
Dockerfile           two-stage, non-root
docker-compose.yml   the API and a cache, for local runs
catalog-info.yaml    this service's entry in the Backstage catalog
```

## Known limits

- A fresh session database has no users, and `scripts/` is not part of the image, so in the
  cluster nobody can log in with a password. Services and Backstage are not affected: they use
  ServiceAccount tokens, and Backstage is the admin. Password login is for local use.
- Whoever holds `JWT_SECRET_KEY` can mint a token with any role. It lives in Secrets Manager
  and in this service's pods only.
- `/metrics` has no authentication; a NetworkPolicy limits who can reach the pods.
- Liveness and readiness both use `/health`, which checks the database. Template services have
  separate `/health/live` and `/health/ready`.
- The audit history of a deleted flag cannot be fetched by flag id any more.
- This image is not attested (template services' images are).

The full history of the platform, every decision and every bug:
[idp-infra/HANDOVER_DOC.md](https://github.com/surya-idp/idp-infra/blob/main/HANDOVER_DOC.md).
