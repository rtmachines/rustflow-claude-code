---
name: rustflow-stack-axum-sqlx
description: Conventions, test patterns and pitfalls for this Rust web stack - Tokio + Axum handlers, SQLx (Postgres) queries and migrations, Askama templates, htmx fragments. Use before adding or changing an endpoint, a query!/query_as! call, a migration or a template, and when reviewing such changes.
---

# Stack: Tokio · Axum · SQLx (Postgres) · Askama · htmx

## Architecture rules
- Build the router in `pub fn app(state: AppState) -> Router` with the state already attached
  (`.with_state(state)`): only `Router<()>` implements `tower::Service`, which tests need for `oneshot`.
- Handlers return `Result<_, AppError>`; `AppError: IntoResponse` logs the cause and returns a generic
  500. A `sqlx::Error` (or any internal error text) never reaches the client.
- Validation errors are a 422 with the form fragment re-rendered, not a 500.
- htmx: a request with `HX-Request: true` gets a fragment; `HX-Boosted` requests and plain requests get
  the full page. One Askama template with `blocks = ["list"]` renders both (`page.as_list().render()`).
- Askama escapes by default - never `|safe` user-controlled data.
- No blocking I/O and no `std::sync::Mutex` guard across `.await` (clippy `await_holding_lock` = deny).

## SQLx rules
- Prefer the checked macros (`query!`, `query_as!`, `query_scalar!`). After changing a query - including
  queries in tests - run `cargo sqlx prepare --workspace -- --all-targets --all-features` and commit
  `.sqlx/`. The `sqlx` gate (`cargo sqlx prepare --check`) fails otherwise.
- Schema change = a new reversible migration: `sqlx migrate add -r <name>`, then `sqlx migrate run`.
  Never edit an applied migration. `sqlx migrate revert` only on a local database (a hook enforces it);
  `sqlx database drop/reset` is blocked - ask the user.
- `#[sqlx::test(migrations = "../../migrations")]` - the path is relative to the crate's
  `CARGO_MANIFEST_DIR`, not the workspace root. Each test gets a fresh database.

## Every new endpoint gets tests (in `crates/<crate>/tests/`)
Test the whole stack in-process with `oneshot` - no server, no ports:

```rust
#![allow(clippy::unwrap_used)] // clippy.toml exempts #[test] fns, not helpers in integration-test files

use axum::{body::Body, http::{Request, StatusCode, header}};
use sqlx::PgPool;
use tower::ServiceExt; // oneshot
use web::{AppState, app};

async fn send(pool: PgPool, req: Request<Body>) -> (StatusCode, String) {
    let res = app(AppState { pool }).oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), usize::MAX).await.unwrap();
    (status, String::from_utf8(bytes.to_vec()).unwrap())
}

#[sqlx::test(migrations = "../../migrations")]
async fn htmx_request_gets_fragment_only(pool: PgPool) {
    let req = Request::get("/books").header("HX-Request", "true").body(Body::empty()).unwrap();
    let (status, html) = send(pool, req).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!html.contains("<html"), "a fragment must not contain the layout");
}
```

For each endpoint cover: full page (no header), fragment (`HX-Request: true`), boosted
(`HX-Request` + `HX-Boosted` -> full page), the happy path persisting data (assert with a
`query_scalar!` - it also lands in `.sqlx/`), validation error status + message in the fragment, and
HTML escaping of user input (`<script>` -> `&lt;script&gt;`).

## Pitfalls (each one bit us)
- **htmx 2 ignores 4xx responses by default**: a 422 validation fragment is not swapped in unless the
  page sets `htmx-config` `responseHandling` allowing `"422"` to swap.
- **`Router<S>` vs `oneshot`**: forgetting `.with_state` in `app()` gives a confusing trait error in tests.
- **Wrong migrations path** in `#[sqlx::test]` -> "migrations not found" at compile time.
- **`query!` in tests** must be in `.sqlx/` too: always `prepare -- --all-targets`.
- **"Blocking waiting for file lock on build directory"**: rust-analyzer and the rustflow hooks share
  `target/`. Point rust-analyzer elsewhere: `"rust-analyzer.cargo.targetDir": true` (uses
  `target/rust-analyzer`).
- **Offline builds**: without `DATABASE_URL` the macros use `.sqlx/`; a stale `.sqlx/` compiles locally
  but fails `prepare --check` in CI (which runs with Postgres and `REQUIRE_SQLX_CHECK=1`).

## Local database for the gates
```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17
echo 'DATABASE_URL=postgres://postgres:postgres@localhost:5432/app_dev' > .env
sqlx database create && sqlx migrate run
```
