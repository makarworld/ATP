import contextlib
import io
import json
import logging
import time

import requests

from atp import settings

logger = logging.getLogger(__name__)


def _post_with_retry(
    url: str,
    data: dict,
    files: dict | None = None,
    timeout: int = 180,
    max_retries: int = 5,
) -> requests.Response:
    """Выполняет POST запрос к Telegram API с обработкой rate limit (429) и повторами."""
    for attempt in range(max_retries):
        try:
            if files:
                for f in files.values():
                    if hasattr(f, "seek"):
                        f.seek(0)
                response = requests.post(url, data=data, files=files, timeout=timeout)
            else:
                response = requests.post(url, data=data, timeout=timeout)

            if response.status_code == 429:
                retry_after = 5
                with contextlib.suppress(Exception):
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
                logger.warning(
                    "Telegram rate limit (429). Sleeping %s s before retry...", retry_after
                )
                time.sleep(retry_after + 1)
                continue

            if response.status_code != 200:
                raise Exception(f"Failed to send Telegram media: {response.text}")

            return response
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            logger.warning(
                "Network error sending to Telegram (attempt %s/%s): %s",
                attempt + 1,
                max_retries,
                e,
            )
            time.sleep(3)
    raise Exception("Max retries exceeded sending to Telegram")


def create_forum_topic(chat_id: str | int, name: str) -> int | None:
    """Создаёт топик форума в супергруппе и возвращает message_thread_id.

    :param chat_id: ID чата/супергруппы
    :param name: Название топика
    :return: message_thread_id или None
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/createForumTopic"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "name": name}, timeout=30)
        data = r.json()
        if data.get("ok"):
            topic_id = data["result"]["message_thread_id"]
            logger.info("Created forum topic '%s' with ID %s", name, topic_id)
            return topic_id
        logger.error("Failed to create forum topic '%s': %s", name, data.get("description"))
        return None
    except Exception as e:
        logger.exception("Error creating forum topic '%s': %s", name, e)
        return None


def get_or_create_topic(topic_name: str, config_attr: str, config_key: str) -> int | None:
    """Возвращает ID топика из настроек или создаёт новый через createForumTopic.

    :param topic_name: Название топика
    :param config_attr: Имя атрибута в settings (например, TELEGRAM_TOPIC_LIKES_ID)
    :param config_key: Имя ключа в settings.conf (например, TELEGRAM_TOPIC_LIKES_ID)
    :return: ID топика (message_thread_id) или None
    """
    topic_id = getattr(settings, config_attr, None)
    if topic_id:
        return int(topic_id)

    if not settings.TELEGRAM_CHAT_ID:
        return None

    new_id = create_forum_topic(settings.TELEGRAM_CHAT_ID, topic_name)
    if new_id:
        setattr(settings, config_attr, new_id)
        settings.set_config_value(config_key, str(new_id))
        return new_id
    return None


def get_video_caption(video) -> str:
    """Возвращает описание видео с датой и ссылкой на TikTok, ограничив 1024 символами."""
    MAX_LENGTH = 1024
    author = f"👤 {video.author}\n" if video.author else ""
    link = f"🔗 https://www.tiktok.com/@/video/{video.id}\n"
    date_str = f"📅 {video.date.strftime('%d.%m.%Y')}\n" if getattr(video, "date", None) else ""
    cut_name = video.name or ""

    fixed_len = len(author) + len(link) + len(date_str)
    if fixed_len + len(cut_name) > MAX_LENGTH:
        diff = (fixed_len + len(cut_name)) - MAX_LENGTH
        cut_name = cut_name[: -diff - 3] + "..."

    caption = f"{author}{cut_name}\n\n{date_str}{link}".strip()
    return caption


def send_media(
    caption: str,
    video: io.BytesIO | str | None = None,
    photos: list[io.BytesIO] | None = None,
    message_thread_id: int | None = None,
) -> dict:
    """Отправляет медиа в Telegram (видео, фото или существующий file_id).

    :param caption: Подпись к медиа
    :param video: BytesIO с видеофайлом ИЛИ строка с Telegram file_id
    :param photos: Список фото в виде BytesIO
    :param message_thread_id: ID топика форума (message_thread_id)
    :return: Результат ответа от Telegram API (dict)
    :raises: Exception с текстом ответа при ошибке
    """
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        raise Exception("Telegram parameters not configured (token or chat ID)")

    base_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"
    chat_id = settings.TELEGRAM_CHAT_ID

    if video:
        # Если передан file_id строкой — отправляем без загрузки файла
        if isinstance(video, str):
            url = f"{base_url}/sendVideo"
            data = {
                "chat_id": chat_id,
                "video": video,
                "caption": caption,
                "supports_streaming": True,
            }
            if message_thread_id:
                data["message_thread_id"] = message_thread_id
            response = _post_with_retry(url, data=data, timeout=60)
            return response.json()["result"]

        media_type = "video"
        media_items = [video]
    elif photos:
        media_type = "photo"
        media_items = photos
    else:
        raise ValueError("Either video or photos must be provided")

    if len(media_items) > 1:
        files = {}
        media = []
        for i, item in enumerate(media_items):
            key = f"{media_type}{i}"
            files[key] = item
            media.append({"type": media_type, "media": f"attach://{key}"})

        media[0]["caption"] = caption
        data = {"chat_id": chat_id, "media": json.dumps(media)}
        if message_thread_id:
            data["message_thread_id"] = message_thread_id
        url = f"{base_url}/sendMediaGroup"
    else:
        files = {media_type: media_items[0]}
        data = {"chat_id": chat_id, "caption": caption}
        if media_type == "video":
            data["supports_streaming"] = True
        if message_thread_id:
            data["message_thread_id"] = message_thread_id
        url = f"{base_url}/send{media_type.capitalize()}"

    response = _post_with_retry(url, data=data, files=files, timeout=180)
    return response.json()["result"]


def edit_media(
    message_id: int,
    caption: str,
    video: io.BytesIO | None = None,
    photo: io.BytesIO | None = None,
    parse_mode: str | None = None,
) -> bool:
    """Редактирует медиа в сообщении Telegram.

    :param message_id: ID сообщения для редактирования
    :param caption: Новая подпись к медиа
    :param video: Видео в виде BytesIO
    :param photo: Фото в виде BytesIO
    :param parse_mode: Режим парсинга (например, "Markdown")
    :return: True если успешно, False иначе
    """
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.error("Telegram parameters not configured (token or chat ID)")
        return False

    if not video and not photo:
        logger.error("Either video or photo must be provided")
        return False

    try:
        media_type = "video" if video else "photo"
        file_obj = video if video else photo

        media = {
            "type": media_type,
            "media": f"attach://{media_type}",
            "caption": caption,
        }
        if parse_mode:
            media["parse_mode"] = parse_mode

        payload = {
            "chat_id": settings.TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "media": json.dumps(media),
        }

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/editMessageMedia"
        response = requests.post(
            url,
            data=payload,
            files={media_type: file_obj},
            timeout=180,
        )

        if response.status_code == 200:
            logger.info("Telegram message media edited successfully.")
            return True
        else:
            logger.error("Failed to edit Telegram message media: %s", response.text)
            return False

    except Exception as e:
        logger.exception("Exception occurred while editing message media: %s", e)
        return False


def discover_chat_id() -> None:
    """Получает ID чата в Telegram и сохраняет его в settings.conf"""
    if not settings.TELEGRAM_BOT_TOKEN or settings.TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"
        response = requests.get(url, timeout=60)
        if response.status_code != 200:
            logger.error("Failed to get Telegram chat ID: %s", response.text)
            return
        for event in response.json()["result"][::-1]:
            if event_message := (
                event.get("message") or event.get("channel_post") or event.get("my_chat_member")
            ):
                chat = event_message["chat"]
                chat_id = str(chat["id"])
                title = chat.get("title") or chat.get("username")
                logger.info("Found chat %s with ID %s", title, chat_id)

                settings.TELEGRAM_CHAT_ID = chat_id
                settings.set_config_value("TELEGRAM_CHAT_ID", chat_id)
                break
        else:
            logger.warning("Can't find chat ID, try sending any message to a channel")
            return

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(
            url,
            data={"chat_id": chat_id, "text": "Удаленные видео будут публиковаться в этом чате"},
            timeout=60,
        )
        if response.status_code == 200:
            logger.info("Message sent successfully.")
        else:
            logger.error(
                f"Failed to send message to chat {title} with ID {chat_id}. Check bot permissions."
            )
            settings.TELEGRAM_CHAT_ID = None
            settings.set_config_value("TELEGRAM_CHAT_ID", "")

    except Exception as e:
        logger.exception("Error occurred while getting Telegram chat ID: %s", e)
        settings.TELEGRAM_CHAT_ID = None
        settings.set_config_value("TELEGRAM_CHAT_ID", "")
