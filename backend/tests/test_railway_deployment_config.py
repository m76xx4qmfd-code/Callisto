import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _toml(path: str) -> dict:
    with (REPO_ROOT / path).open("rb") as handle:
        return tomllib.load(handle)


def test_railway_service_configs_use_dockerfiles_migrations_health_and_retries():
    backend = _toml("backend/railway.toml")
    frontend = _toml("frontend/railway.toml")

    assert backend["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}
    assert backend["deploy"] == {
        "preDeployCommand": ["alembic upgrade head"],
        "healthcheckPath": "/health",
        "healthcheckTimeout": 300,
        "restartPolicyType": "ON_FAILURE",
        "restartPolicyMaxRetries": 3,
    }
    assert frontend["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}
    assert frontend["deploy"] == {
        "healthcheckPath": "/healthz",
        "healthcheckTimeout": 120,
        "restartPolicyType": "ON_FAILURE",
        "restartPolicyMaxRetries": 3,
    }


def test_frontend_image_uses_railway_nginx_template_and_private_backend_proxies():
    dockerfile = _read("frontend/Dockerfile")
    nginx = _read("frontend/nginx.conf")

    assert "ENV PORT=3000" in dockerfile
    assert "BACKEND_HOST=backend" in dockerfile
    assert "BACKEND_PORT=8000" in dockerfile
    assert "COPY nginx.conf /etc/nginx/templates/default.conf.template" in dockerfile
    assert "listen ${PORT};" in nginx
    assert "location = /healthz" in nginx
    assert "location /ws" in nginx
    assert "location /mcp" in nginx
    assert "location /api" in nginx
    assert nginx.count("proxy_pass http://${BACKEND_HOST}:${BACKEND_PORT};") == 3
    assert nginx.count('add_header Cache-Control "no-store" always;') == 2


def test_nginx_security_headers_and_backend_dynamic_port_are_wired():
    nginx = _read("frontend/nginx.conf")
    backend_dockerfile = _read("backend/Dockerfile")

    for directive in (
        "server_tokens off;",
        'add_header X-Content-Type-Options "nosniff" always;',
        'add_header X-Frame-Options "DENY" always;',
        'add_header Referrer-Policy "same-origin" always;',
        'add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;',
    ):
        assert directive in nginx
    assert '${PORT:-8000}' in backend_dockerfile
    assert 'CMD ["sh", "-c", "exec uvicorn' in backend_dockerfile


def test_runbook_requires_both_railway_domain_records_and_private_backend():
    runbook = _read("docs/deployment/CALLISTO_TERMINAL_RAILWAY.md")

    assert "CNAME routing target and a TXT verification record" in runbook
    assert "Create **both** records" in runbook
    assert "DNS-only while Railway validates ownership" in runbook
    assert "Full (strict)" in runbook
    assert "Never expose the backend or database" in runbook
    assert "https://callistoterminal.ai" in runbook
    assert "europaterminal.ai" not in runbook


def test_backend_canonical_origin_is_callisto_terminal_only():
    main = _read("backend/main.py")

    assert 'settings.PUBLIC_APP_ORIGIN != "https://callistoterminal.ai"' in main
    assert "europaterminal.ai" not in main


def test_railway_upload_keeps_tracked_frontend_country_reference():
    gitignore = _read(".gitignore")

    assert "!frontend/src/data/" in gitignore
    assert "!frontend/src/data/countryReference.json" in gitignore
    assert (REPO_ROOT / "frontend/src/data/countryReference.json").is_file()
