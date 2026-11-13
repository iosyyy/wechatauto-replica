# Linux Reader Foundation Implementation Plan

> For agentic workers: execute this approved phase in order using the executing-plans
> workflow. Later live-client and LangBot phases have separate acceptance boundaries.

**Goal:** Make the maintained fork usable for account-bound Linux database reading
with explicitly supplied, cryptographically verified keys, preserving Windows behavior.

**Architecture:** Lazy public exports keep the GUI out of database consumers.
Platform-specific account discovery and key loading are isolated from the existing
SQLCipher reader. The real upstream database/listener code is exercised with
encrypted synthetic SQLite fixtures.

**Tech Stack:** Python 3.9+, unittest, SQLite, cryptography, Linux/XDG filesystem.

## Task 1: Cross-platform package imports

Files: `wechatauto/__init__.py`, `pyproject.toml`, `wechatauto/db.py`,
`tests/test_platform_imports.py`.

- [ ] Add subprocess tests for `import wechatauto` and
  `from wechatauto import WeChatDB`, asserting no Windows GUI modules load.
- [ ] Run `python -m unittest discover -s tests -p test_platform_imports.py -v`;
  observe failure on the unchanged upstream package.
- [ ] Preserve every public export using lazy import mapping. Keep top-level
  `Listener` mapped to the upstream compatibility type; callers use `db.Listener`.
- [ ] Guard Windows native-library initialization. Add platform markers to the
  three Windows-only dependencies. Do not advertise Linux GUI support.
- [ ] Re-run the import tests and verify the dependency markers for both platforms.

## Task 2: Linux account and key backend

Files: `wechatauto/platforms/__init__.py`, `wechatauto/platforms/linux.py`,
`wechatauto/db.py`, `tests/test_linux_reader.py`.

- [ ] Add real temporary account directories and encrypted SQLite fixtures.
- [ ] Cover explicit/XDG data-root discovery, multiple-account rejection, key
  validation, missing keys, wrong-account keys, hostile relative paths, cache
  permissions, real contact/message queries and listener restart watermark.
- [ ] Observe new Linux tests failing before changing the reader.
- [ ] Add Linux-only dispatch for discovery/key loading; leave the Windows key
  extraction and account healing branches intact.
- [ ] Read supplied key files without following symlinks; do not allow database
  paths outside the selected account. Validate cache candidates against each DB.
- [ ] Keep Linux master-key/cache state private and do not log key material.
- [ ] Run `python -m unittest discover -s tests -p 'test_linux*.py' -v`.

## Task 3: Documentation and reproducible verification

Files: `docs/linux.md`, `README.md`,
`docs/superpowers/specs/2026-11-13-linux-native-design.md`.

- [ ] Document exact Linux source-install and private key-file reading commands.
- [ ] State that the phase does not provide automatic Linux key extraction or GUI
  sending and that client-specific compatibility remains unverified.
- [ ] Run the focused suite in a clean Linux environment if available; record
  platform and scope. Windows-only suites remain a separate validation task.
- [ ] Self-review the complete diff for accidental API removal, account fallback,
  unsafe paths, secret logging and misleading completion claims.
- [ ] Commit with `feat:` and push the implementation branch to the personal fork.

## Next acceptance steps

1. Verify a real Linux client version, target account, schema and keys.
2. Implement and exercise a Linux sender with target verification and no blind retry.
3. Add durable LangBot bridging and verify restart/retry/duplicate handling before
   switching an existing live bot route.
