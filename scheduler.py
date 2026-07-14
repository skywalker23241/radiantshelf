import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler = None


def init_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler()

    from config import Config

    def daily_check():
        with app.app_context():
            from webhook import process_all_users
            process_all_users()

    def weekly_skin_refresh():
        with app.app_context():
            from skin_cache import refresh_skin_cache
            refresh_skin_cache()

    def rebind_reminder():
        with app.app_context():
            from webhook import send_rebind_reminders

            result = send_rebind_reminders()
            if result["attempted"]:
                logger.info(
                    "重新绑定提醒完成: %s 成功, %s 失败",
                    result["success"],
                    result["failed"],
                )

    _scheduler.add_job(
        daily_check,
        CronTrigger(
            hour=Config.CHECK_HOUR,
            minute=Config.CHECK_MINUTE,
            timezone=Config.TIMEZONE,
        ),
        id="daily_store_check",
        name="每日商店检查",
        replace_existing=True,
    )

    _scheduler.add_job(
        rebind_reminder,
        CronTrigger(minute="*", timezone=Config.TIMEZONE),
        id="daily_rebind_reminder",
        name="Riot 重新绑定提醒",
        replace_existing=True,
    )

    _scheduler.add_job(
        weekly_skin_refresh,
        CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=Config.TIMEZONE),
        id="weekly_skin_refresh",
        name="每周皮肤缓存刷新",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "定时任务已启动: 每天 %s:%02d 检查商店，按用户时区发送重新绑定提醒",
        Config.CHECK_HOUR,
        Config.CHECK_MINUTE,
    )
    return _scheduler


def get_scheduler():
    return _scheduler
