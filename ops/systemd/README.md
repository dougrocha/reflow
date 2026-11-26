# The nightly runner's systemd units

`reflow-nightly.timer` starts `reflow-nightly.service` every 15 minutes. The service runs
`scripts/nightly`, which runs the slow jobs on the newest commit of origin/main that has no
complete report yet, and does nothing when that commit has one. Reports go to
`~/.cache/reflow/nightly/<commit>/`, and `latest` points at the newest. See the docstring of
`scripts/nightly` for how a run converges.

The units expect the main checkout at `~/dev/reflow`; the service runs origin/main's
`scripts/nightly`, whatever the checkout has checked out. Install them as user units:

```sh
install -Dm644 -t ~/.config/systemd/user ops/systemd/reflow-nightly.service ops/systemd/reflow-nightly.timer
systemctl --user daemon-reload
systemctl --user enable --now reflow-nightly.timer
loginctl enable-linger "$USER"   # optional: run while nobody is logged in
```

Read the state with `scripts/nightly status`, `systemctl --user list-timers reflow-nightly.timer`
and `journalctl --user -u reflow-nightly`. Trigger a run by hand with
`systemctl --user start --no-block reflow-nightly`. Remove the units with
`systemctl --user disable --now reflow-nightly.timer` and by
removing the two files from `~/.config/systemd/user`. Install them again after a change here.
