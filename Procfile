web: gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:${PORT:-8000} --access-logfile - --error-logfile - --timeout 60 --graceful-timeout 30
worker: arq app.workers.main.WorkerSettings
release: alembic upgrade head
