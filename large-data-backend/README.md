# SIWES 1 GB dataset backend

This is the server component for datasets too large to analyse in GitHub Pages. It imports CSVs into DuckDB on a Render persistent disk and supports previews, replacements, and server-side edits.

## Deploy on Render

1. Create a **new GitHub repository** named `siwes-large-data-api`.
2. Upload the files from this `large-data-backend` folder to that repository's root. Keep this separate from the GitHub Pages website repository.
3. In Render, choose **New → Blueprint** and select that repository. Render reads `render.yaml`.
4. Use a paid plan with a persistent disk. The included 25 GB disk leaves safe working room for a 1 GB CSV and its DuckDB database. Increase it as your collection grows.
5. Add these Render environment variables:
   - `SUPABASE_URL` — your existing Supabase project URL.
   - `SUPABASE_SERVICE_ROLE_KEY` — from Supabase **Settings → API**. This is server-only; never add it to GitHub Pages.
6. Deploy and copy the Render service URL, for example `https://siwes-large-data-api.onrender.com`.

## Supabase Storage

Create a private `large-datasets` bucket. In **Storage Settings**, raise the global and bucket upload limits to at least 1 GB. The website must use a resumable TUS upload to send the source file directly to this bucket, then provide a short-lived signed URL to the Render API.

The backend is intentionally separate: GitHub Pages is static and cannot safely hold a service-role secret or process a 1 GB file.
