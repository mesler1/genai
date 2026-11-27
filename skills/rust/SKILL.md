# Rust: house rules and the gotchas that bite

- Build and check with Cargo: `cargo build`, `cargo test`, `cargo clippy -- -D warnings`, `cargo fmt`. Propose them one at a time.
- Errors: return `Result` and use `?`; `anyhow` for applications, `thiserror` for libraries. No `unwrap()` or `expect()` outside tests and truly impossible cases (and say why in the `expect` message).
- No `unsafe` unless the user asks, and then the smallest block with a `// SAFETY:` comment saying why it holds.
- Prefer borrowing (`&str`, `&[T]`) in arguments over owned types; clone only where ownership is really needed, and say so.
- Don't add dependencies casually: name the crate and why, and prefer what the project already uses. Offline or air-gapped clusters may need `cargo vendor`.
- Integer overflow panics in debug builds and wraps in release: use `checked_*`, `saturating_*` or `wrapping_*` where it matters.
- Iterators over index loops; `for (i, x) in v.iter().enumerate()` when the index is needed.
- On a cluster, `~/.cargo` in a small home directory fills fast: suggest `CARGO_HOME` and `CARGO_TARGET_DIR` in scratch or project space.
