import io
import logging
import os
from pathlib import Path

from atp import crud, settings
from atp.check_availability import check_services_availability
from atp.database import get_db_session
from atp.models import Video, VideoStatus
from atp.settings import DOWNLOADS_DIR, HOPE_MODE
from atp.telegram import get_or_create_topic, get_video_caption, send_media, update_topic_info
from atp.tiktok import download_video

logger = logging.getLogger(__name__)


def upload_video_to_topics(db, video: Video, video_path: Path) -> str | None:
    """Выгружает видео в топики Telegram (All Likes / All Favorites).

    При DELETE_LOCAL_AFTER_UPLOAD удаляет локальный файл после сохранения tg_file_id.
    """
    if (
        not settings.TELEGRAM_TOPICS_MODE
        or not settings.TELEGRAM_BOT_TOKEN
        or not settings.TELEGRAM_CHAT_ID
    ):
        return None

    likes_thread_id = get_or_create_topic(
        "All Likes", "TELEGRAM_TOPIC_LIKES_ID", "TELEGRAM_TOPIC_LIKES_ID"
    )
    favs_thread_id = get_or_create_topic(
        "All Favorites", "TELEGRAM_TOPIC_FAVORITES_ID", "TELEGRAM_TOPIC_FAVORITES_ID"
    )

    caption = get_video_caption(video)
    tg_file_id = video.tg_file_id
    likes_msg_id = video.tg_likes_msg_id
    favs_msg_id = video.tg_favs_msg_id

    # 1. Выгрузка в All Likes
    if video.liked and not likes_msg_id:
        try:
            if tg_file_id:
                res = send_media(
                    caption=caption,
                    video=tg_file_id,
                    message_thread_id=likes_thread_id,
                )
            elif video_path.exists():
                if video_path.stat().st_size > settings.TELEGRAM_MAX_VIDEO_SIZE:
                    logger.warning(
                        "Video %s exceeds Telegram 50MB limit, skipping upload", video.id
                    )
                    res = None
                else:
                    with open(video_path, "rb") as f:
                        res = send_media(
                            caption=caption,
                            video=io.BytesIO(f.read()),
                            message_thread_id=likes_thread_id,
                        )
            else:
                res = None
            if res:
                likes_msg_id = res.get("message_id")
                if not tg_file_id and res.get("video"):
                    tg_file_id = res["video"]["file_id"]
                logger.info("Uploaded video %s to 'All Likes' (msg_id: %s)", video.id, likes_msg_id)
        except Exception as e:
            logger.error("Failed to upload video %s to All Likes: %s", video.id, e)

    # 2. Выгрузка в All Favorites
    if video.saved and not favs_msg_id:
        try:
            if tg_file_id:
                res = send_media(
                    caption=caption,
                    video=tg_file_id,
                    message_thread_id=favs_thread_id,
                )
            elif video_path.exists():
                if video_path.stat().st_size > settings.TELEGRAM_MAX_VIDEO_SIZE:
                    logger.warning(
                        "Video %s exceeds Telegram 50MB limit, skipping upload", video.id
                    )
                    res = None
                else:
                    with open(video_path, "rb") as f:
                        res = send_media(
                            caption=caption,
                            video=io.BytesIO(f.read()),
                            message_thread_id=favs_thread_id,
                        )
            else:
                res = None
            if res:
                favs_msg_id = res.get("message_id")
                if not tg_file_id and res.get("video"):
                    tg_file_id = res["video"]["file_id"]
                logger.info(
                    "Uploaded video %s to 'All Favorites' (msg_id: %s)", video.id, favs_msg_id
                )
        except Exception as e:
            logger.error("Failed to upload video %s to All Favorites: %s", video.id, e)

    # Сохраняем в БД
    crud.update_video(
        db,
        video=video,
        tg_file_id=tg_file_id,
        tg_likes_msg_id=likes_msg_id,
        tg_favs_msg_id=favs_msg_id,
        update_last_checked=False,
    )

    # Если включено удаление локального файла и файл успешно выгружен
    if settings.DELETE_LOCAL_AFTER_UPLOAD and tg_file_id and video_path.exists():
        try:
            os.remove(video_path)
            logger.info(
                "Deleted local file %s to save disk space (tg_file_id saved)",
                video_path.name,
            )
        except Exception as e:
            logger.warning("Failed to delete local file %s: %s", video_path, e)

    return tg_file_id


def download_new_videos() -> None:
    """Скачивает новые видео TikTok"""
    db = get_db_session()

    try:
        if not check_services_availability():
            return

        total_saved_this_round = 0

        # Если включен режим топиков, сначала выгрузим уже скачанные локальные видео без file_id
        if settings.TELEGRAM_TOPICS_MODE:
            pending_upload = (
                db.query(Video)
                .filter(Video.status == VideoStatus.SUCCESS, Video.tg_file_id.is_(None))
                .all()
            )
            if pending_upload:
                logger.info(
                    "Found %s downloaded videos pending Telegram upload",
                    len(pending_upload),
                )
                for v in pending_upload:
                    v_path = Path(DOWNLOADS_DIR) / f"{v.id}.mp4"
                    if v_path.exists():
                        res = upload_video_to_topics(db, v, v_path)
                        if res:
                            total_saved_this_round += 1
                            if total_saved_this_round % 10 == 0:
                                update_topic_info(crud.get_stats(db))

        videos = crud.get_videos(db, status=[VideoStatus.NEW])
        if HOPE_MODE:
            logger.info(
                "HOPE_MODE is enabled, will try to download failed videos. This may take a while."
            )
            videos.extend(crud.get_videos(db, status=[VideoStatus.FAILED]))
        if not videos:
            if settings.TELEGRAM_TOPICS_MODE and total_saved_this_round > 0:
                logger.info(
                    "Updating topic info after saving backlog videos (%s saved)",
                    total_saved_this_round,
                )
                update_topic_info(crud.get_stats(db))
            return

        logger.info("Found %s new%s videos", len(videos), " or failed" if HOPE_MODE else "")

        success_count = 0
        for i, video in enumerate(videos):
            logger.info("Downloading video %s/%s: %s", i + 1, len(videos), video.id)

            if not (result := download_video(video)):
                continue
            success = not result.deleted_reason
            status = VideoStatus.SUCCESS if success else VideoStatus.FAILED
            crud.update_video(
                db,
                video=video,
                status=status,
                name=result.name,
                author=result.author,
                type=result.type,
                deleted_reason=result.deleted_reason,
            )

            if success:
                success_count += 1
                logger.info("Successfully downloaded video %s", video.id)
                if settings.TELEGRAM_TOPICS_MODE:
                    video_path = Path(DOWNLOADS_DIR) / f"{video.id}.mp4"
                    res = upload_video_to_topics(db, video, video_path)
                    if res:
                        total_saved_this_round += 1
                        if total_saved_this_round % 10 == 0:
                            update_topic_info(crud.get_stats(db))
            else:
                logger.warning("Failed to download video %s", video.id)

        logger.info("Downloaded %s/%s videos", success_count, len(videos))
        if new_left := crud.get_videos(db, status=[VideoStatus.NEW]):
            logger.info("%s videos with status `new` remaining", len(new_left))
        if HOPE_MODE:
            logger.info("Don't forget to disable HOPE_MODE in settings.conf!")

        # Обновляем info-сообщения после сохранения последнего видео в круге
        if settings.TELEGRAM_TOPICS_MODE and total_saved_this_round > 0:
            logger.info(
                "Updating topic info after saving last video in round (%s saved)",
                total_saved_this_round,
            )
            update_topic_info(crud.get_stats(db))

    except Exception as e:
        logger.exception("Error downloading videos: %s", e)
    finally:
        db.close()


if __name__ == "__main__":
    download_new_videos()
