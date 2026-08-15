from celery import Celery

from app.core.config import settings


def create_celery_app() -> Celery:
    application = Celery("oncall", broker=settings.redis_url, backend=settings.redis_url)
    application.conf.task_always_eager = settings.celery_task_always_eager
    application.conf.task_ignore_result = False
    application.conf.result_expires = 3600
    application.conf.task_serializer = "json"
    application.conf.result_serializer = "json"
    application.conf.accept_content = ["json"]
    application.conf.include = ["app.tasks.workers.kb_sync", "app.tasks.workers.notifications"]
    return application


celery_app = create_celery_app()