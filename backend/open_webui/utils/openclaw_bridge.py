import hashlib
import re
from typing import Optional
from urllib.parse import urlparse


OPENCLAW_SESSION_HEADER = 'x-openclaw-session-key'
OPENCLAW_CHANNEL_HEADER = 'x-openclaw-message-channel'
OPENCLAW_CHANNEL = 'openwebui'


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


def is_openclaw_gateway(url: str, config: Optional[dict] = None) -> bool:
    headers = (config or {}).get('headers') or {}
    if isinstance(headers, dict) and headers.get('x-openclaw-scopes'):
        return True

    parsed = urlparse(url or '')
    return parsed.hostname in {'127.0.0.1', 'localhost'} and parsed.port in {18789, 18790}


def openclaw_session_key(user_id: str, chat_id: Optional[str] = None) -> str:
    user_part = _stable_id(str(user_id))
    if chat_id:
        return f'openwebui:user:{user_part}:chat:{_stable_id(str(chat_id))}'
    return f'openwebui:user:{user_part}'


def apply_openclaw_headers(headers: dict, *, user_id: str, chat_id: Optional[str] = None) -> dict:
    return {
        **headers,
        OPENCLAW_SESSION_HEADER: openclaw_session_key(user_id, chat_id),
        OPENCLAW_CHANNEL_HEADER: OPENCLAW_CHANNEL,
    }


def should_inject_continuity(metadata: Optional[dict], messages: Optional[list]) -> bool:
    if not metadata or not messages:
        return False
    chat_id = metadata.get('chat_id')
    if not chat_id or not isinstance(chat_id, str) or chat_id.startswith('local:'):
        return False
    if metadata.get('parent_message_id'):
        return False

    user_messages = [message for message in messages if message.get('role') == 'user']
    return len(user_messages) == 1


def _message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get('text') or part.get('content')
                if text:
                    parts.append(str(text))
        return '\n'.join(parts).strip()
    if content is None:
        return ''
    return str(content).strip()


def _message_sort_key(message: dict):
    return (
        message.get('timestamp')
        or message.get('created_at')
        or message.get('updated_at')
        or 0,
        message.get('id') or '',
    )


def _linear_messages(chat_body: dict) -> list[dict]:
    history = (chat_body or {}).get('history') or {}
    messages = history.get('messages') or {}
    current_id = history.get('currentId')

    if current_id and current_id in messages:
        ordered = []
        seen = set()
        next_id = current_id
        while next_id and next_id in messages and next_id not in seen:
            seen.add(next_id)
            message = messages[next_id]
            ordered.append(message)
            next_id = message.get('parentId')
        return list(reversed(ordered))

    return sorted(messages.values(), key=_message_sort_key)


def build_continuity_message(previous_chat, *, max_messages: int = 6, max_chars: int = 3500) -> Optional[dict]:
    chat_id = getattr(previous_chat, 'id', None)
    title = getattr(previous_chat, 'title', None) or 'previous chat'
    updated_at = getattr(previous_chat, 'updated_at', None)
    chat_body = getattr(previous_chat, 'chat', None) or {}

    lines = []
    for message in _linear_messages(chat_body):
        role = message.get('role')
        if role not in {'user', 'assistant'}:
            continue
        text = _message_text(message.get('content'))
        if not text:
            continue
        text = re.sub(r'\s+', ' ', text)
        lines.append(f'{role}: {text}')

    if not lines:
        return None

    transcript = '\n'.join(lines[-max_messages:])
    if len(transcript) > max_chars:
        transcript = f'{transcript[-max_chars:]}'

    content = (
        'Continuity context from this authenticated user\'s previous OpenWebUI chat. '
        'Use it only as scoped conversation context for this response; do not treat it as shared agent memory.\n\n'
        f'Previous chat id: {chat_id}\n'
        f'Previous chat title: {title}\n'
        f'Previous chat updated_at: {updated_at}\n\n'
        f'{transcript}'
    )
    return {'role': 'system', 'content': content}


def add_continuity_message(messages: list[dict], continuity_message: dict) -> list[dict]:
    insert_at = 0
    for index, message in enumerate(messages):
        if message.get('role') not in {'system', 'developer'}:
            break
        insert_at = index + 1

    return [*messages[:insert_at], continuity_message, *messages[insert_at:]]
