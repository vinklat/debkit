# debkit

Command-line tool for building, inspecting, and mirroring Debian package repositories on a local
filesystem, S3-compatible object storage, or HTTP(S) servers, with GPG-signed Release files,
streamed checksum verification, and transactional writes you can revert.

## Technology

Python 3.13+, [txstore](https://github.com/vinklat/txstore), python-debian, python-gnupg, humanize,
pytest, Ruff, and mypy.

## License

MIT → [LICENSE](LICENSE).
