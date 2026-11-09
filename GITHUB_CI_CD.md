# GitHub Actions and Docker Hub

The workflow in `.github/workflows/ci-cd.yml` runs the maintained Python test
suite and validates the configuration on Python 3.12. It then builds the
production Docker image. Pull requests and manual runs build the image without
publishing it. A push to `main` or a version tag such as `v1.2.0` publishes it
to Docker Hub, after the tests pass.

The runtime image is built from the root `Dockerfile`. It contains `app/`,
`main.py`, `config.json`, and runtime dependencies. `.dockerignore` keeps
`testcode/`, `tests/`, test reports, and other development files out of that
image. Test code remains in the repository and is used by the separate CI job.

## GitHub repository setup

1. Create a Docker Hub repository, for example `fal-processor`.
2. In the GitHub repository, open **Settings > Secrets and variables > Actions**.
3. Add repository variables:
   - `DOCKERHUB_USERNAME`: your Docker Hub username.
   - `DOCKERHUB_REPOSITORY`: the Docker Hub repository name, without the username.
4. Add a repository secret:
   - `DOCKERHUB_TOKEN`: a Docker Hub access token with permission to push to that repository.
5. Ensure the GitHub default branch is named `main`, or edit the `main` branch
   entries in `.github/workflows/ci-cd.yml` to match your default branch.
6. Push the project to GitHub. The workflow runs on pull requests targeting
   `main`, pushes to `main`, version tags beginning with `v`, and manual runs.

If this folder is not connected to a GitHub repository yet, create an empty
GitHub repository, then run these commands from the project folder in
PowerShell. Replace the example repository name with your own:

```powershell
git init -b main
git add .
git commit -m "Add Fal Processor"
git remote add origin https://github.com/YOUR_USER/YOUR_REPOSITORY.git
git push -u origin main
```

If you already created a GitHub repository with a README or other initial
commit, connect and pull that history before pushing this local project.

## Published image tags

- Push to `main`: `latest`, `main`, and a commit SHA tag.
- Push a version tag such as `v1.2.0`: `v1.2.0` and a commit SHA tag.

After publishing, pull and run the image with your Compose configuration. Set
`SOAP_PASSWORD`, database credentials, and the real SOAP endpoint through your
deployment environment. Do not put production credentials in `config.json` or
commit a `.env` file.

## Local checks

From the project root, the equivalent pre-publish checks are:

```powershell
$env:SOAP_PASSWORD = 'ci-test-only'
.\.venv\Scripts\python.exe main.py --config config.json --validate-config
.\.venv\Scripts\python.exe testcode\run_tests.py
.\.venv\Scripts\python.exe -m pip check
docker build -t fal-processor:local .
```

The workflow intentionally does not run the opt-in tests against live PostgreSQL
or Redis. Those require dedicated test services and explicit connection settings.
