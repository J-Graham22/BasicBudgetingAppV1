# Deployment directions

Recommended setup: run the app on your own Linux machine as a background service, and add Tailscale if you want to use it from your phone.

- No server, domain, or login system needed.
- Your SimpleFIN bank credential stays on your machine.

Avoid cloud hosting (Fly.io, Railway, etc.) for now:

- The app has no login, so exposing it publicly would mean adding authentication first.
- You'd also need persistent storage for the SQLite database.
- Your bank credential would live on someone else's server.

## Step 1: Keep it running in the background (~5 minutes)

Create the service file:

```sh
mkdir -p ~/.config/systemd/user
```

Save this as `~/.config/systemd/user/kakeibo.service`:

```ini
[Unit]
Description=Kakeibo budget app
After=network-online.target

[Service]
WorkingDirectory=/home/punx/Projects/BasicBudgetingAppV1
ExecStart=/home/punx/Projects/BasicBudgetingAppV1/run.sh
Restart=on-failure

[Install]
WantedBy=default.target
```

Enable and start it:

```sh
systemctl --user daemon-reload
systemctl --user enable --now kakeibo
loginctl enable-linger $USER   # keeps it running when you're logged out
```

The app is now:

- available at http://127.0.0.1:5000
- started automatically at boot
- syncing your bank every 6 hours (syncing only happens while the app is running)

Useful commands:

```sh
systemctl --user status kakeibo      # is it running?
systemctl --user restart kakeibo     # apply code changes
journalctl --user -u kakeibo -f      # follow logs
```

## Step 2 (optional): Phone access with Tailscale (~10 minutes)

1. Install and start Tailscale on this machine:

   ```sh
   sudo pacman -S tailscale
   sudo systemctl enable --now tailscaled
   sudo tailscale up
   ```

2. Install the Tailscale app on your phone and sign in to the same account.

3. Make the app available on your tailnet over HTTPS:

   ```sh
   sudo tailscale serve --bg 5000
   ```

4. On your phone, open the `https://<machine-name>.<tailnet>.ts.net` address it prints.

Only devices signed into your Tailscale account can reach that address, so the missing login is fine.

> **Do not use `tailscale funnel`.** That makes the app public on the internet.

**The catch:** the app is only reachable while this machine is on. If that's a problem, move the project folder to an always-on device (a Raspberry Pi, an old PC) and repeat these steps there. Update the paths in the service file to match the new location.

## Backups

All data lives in one file: `data/budget.db`. A daily copy is enough.

```sh
mkdir -p ~/Backups
crontab -e
```

Add this line:

```sh
0 3 * * * cp ~/Projects/BasicBudgetingAppV1/data/budget.db ~/Backups/budget-$(date +\%F).db
```

The backups hold your SimpleFIN credential, so keep them private.

To restore a backup:

1. Stop the app: `systemctl --user stop kakeibo`
2. Copy a backup over `data/budget.db`.
3. Start it again: `systemctl --user start kakeibo`
