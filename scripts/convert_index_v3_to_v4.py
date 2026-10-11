"""Compatibility entry point; prefer ``autodj migrate-index --help``."""

from autodj.index_migration import main

if __name__ == "__main__":
    raise SystemExit(main())
