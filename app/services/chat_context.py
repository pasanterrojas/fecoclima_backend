"""Keep user observations, visual evidence and calculated parcel facts distinct."""
from typing import Any


def widget_context(results: dict, window_hours: int) -> dict[str, str]:
    return {
        purpose: str(result['markdown'])
        for purpose, result in results.items()
        if purpose != 'CROP_CHAT' and isinstance(result, dict)
        and result.get('window_hours') == window_hours and result.get('markdown')
    }


def conversation_observations(history: list[dict] | None) -> list[dict[str, Any]]:
    """Unverified user statements, not automatically accepted agronomic facts."""
    return [
        {'statement': str(item.get('content') or '')[:1000],
         'transcription': str(item.get('transcription') or '')[:1000],
         'photo_count': len(item.get('images') or []),
         'source': 'user_statement_not_verified'}
        for item in (history or [])[-100:]
        if item.get('role') == 'user'
    ]
