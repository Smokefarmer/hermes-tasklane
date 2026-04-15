# Release Checklist

Before telling teammates this is production-ready:

- [ ] `python3 -m pytest -q` passes
- [ ] `python3 -m compileall src` passes
- [ ] `bash -n scripts/install.sh` passes
- [ ] README install steps still work on a clean machine
- [ ] systemd timers install and start cleanly
- [ ] one real repo has been tested end-to-end with sync + reconcile
- [ ] GitHub repo is public and CI is green
