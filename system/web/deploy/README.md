# HTTPS Deployment

The included templates use systemd to run the application on `127.0.0.1:8010` and Nginx to serve it over HTTPS. Configure DNS and TLS for your host. The Nginx template uses `agendex.icu` and redirects `www.agendex.icu` to the main domain. Adapt both domains and the certificate paths for your deployment.

## Files and configuration

| File or directory | Location |
| --- | --- |
| [systemd unit](agendex.service) | `/etc/systemd/system/agendex.service` |
| [Nginx template](nginx.conf) | `/etc/nginx/sites-available/agendex.icu`, enabled through `sites-enabled` |
| Backend and built frontend | `/opt/agendex/app/system/web/backend/` and `/opt/agendex/app/system/web/dist/` |
| Python environment | `/opt/agendex/venv/`, installed from `backend/requirements.lock` |
| SQLite database | `/var/lib/agendex/agendex.sqlite3`, writable by the `agendex` service account |
| TLS certificate and key | `/etc/nginx/ssl/agendex.icu/fullchain.pem` and `privkey.pem`, with the private key readable only by root |
| Environment file | `/etc/agendex/agendex.env`, readable only by root |

Build the frontend with `npm --prefix system/web run build`. Use the [data pipeline](../../pipeline/README.md) to initialize the catalog. Use the SQLite backup API to copy an existing database while it is in use.

Set `AGENDEX_DB_PATH`, `AGENDEX_AUTH_ENABLED=1`, `AGENDEX_AUTH_USERNAME`, `AGENDEX_AUTH_PASSWORD_HASH`, `AGENDEX_SESSION_SECRET`, and `AGENDEX_PUBLIC_ORIGIN` in the environment file. Set the public origin to the exact HTTPS origin served by Nginx. Generate the password hash with `agendex.auth.hash_password` and use a separate random value for the session secret.

Set `OPENROUTER_API_KEY` to enable Jev. For GLM, set `AGENDEX_GLM_BASE_URL` to an OpenAI-compatible HTTPS API base URL and set `AGENDEX_GLM_API_KEY`. The app appends `/chat/completions` to the base URL. When upgrading from a version that supplied the GLM base URL internally, add it to the environment file before restarting.

## Updates and verification

Rebuild the frontend and update the application files. Wait for active classification and policy generation requests to finish before restarting the service. Use `journalctl -u agendex` to check the service logs.

Run `nginx -t` before reloading Nginx. Verify HTTPS and sign-in, check that the repository catalog loads after sign-in, and confirm that the API returns HTTP 401 after sign-out.
